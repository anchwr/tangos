import argparse
import pdb
import random
import sys
import time
import traceback

import numpy as np
import sqlalchemy
import sqlalchemy.exc
import sqlalchemy.orm

from .. import config, core, live_calculation, parallel_tasks, properties
from ..cached_writer import insert_list
from ..log import logger
from ..parallel_tasks import accumulative_statistics
from ..util import proxy_object, terminalcontroller, timing_monitor
from ..util.check_deleted import check_deleted
from . import GenericTangosTool


class AttributableDict(dict):
    pass


class PropertyWriter(GenericTangosTool):
    tool_name = "write"
    tool_description = "Calculate properties and write them into the tangos database"

    def __init__(self):
        self.redirect = terminalcontroller.redirect
        self._writer_timeout = config.PROPERTY_WRITER_MAXIMUM_TIME_BETWEEN_COMMITS
        self._writer_minimum = config.PROPERTY_WRITER_MINIMUM_TIME_BETWEEN_COMMITS
        self._current_timestep_id = None
        self._loaded_timestep = None
        self._loaded_halo_id = None
        self._loaded_halo = None

    @classmethod
    def add_parser_arguments(self, parser):
        parser.add_argument('properties', action='store', nargs='+',
                            help="The names of the halo properties to calculate")
        parser.add_argument('--sims', '--for', action='store', nargs='*',
                            metavar='simulation_name',
                            help='Specify a simulation (or multiple simulations) to run on')
        parser.add_argument('--latest', action='store_true',
                            help='Run only on the latest timesteps')
        parser.add_argument('--timesteps-matching', action='append', type=str,
                            help='Run only on timesteps with extensions matching the specified string. Multiple timesteps may be specified.')
        parser.add_argument('--force', action='store_true',
                            help='Run calculations even if a value is already stored in the database')
        parser.add_argument('--debug', action='store_true',
                            help='Run in debug mode: print rather than creating database entries')
        parser.add_argument('--catch', '--pdb', action='store_true',
                            help='Launch the python debugger when a calculation error occurs')
        parser.add_argument('--backwards', action='store_true',
                            help='Process low-z timesteps first')
        parser.add_argument('--random', action='store_true',
                            help='Process timesteps in random order')
        parser.add_argument('--with-prerequisites', action='store_true',
                            help='Automatically calculate any missing prerequisites for the properties')
        parser.add_argument('--no-resume', action='store_true',
                            help="Prevent resumption from a previous calculation, even if tangos thinks it's possible")
        parser.add_argument('--load-mode', action='store', choices=['all', 'partial', 'server', 'server-partial', 'server-shared-mem'],
                            required=False, default=None,
                            help="Select a load-mode: " \
                                 "  --load-mode partial:           each processor attempts to load only the data it needs; " \
                                 "  --load-mode server:            a server process manages the data;"
                                 "  --load-mode server-partial:    a server process figures out the indices to load, which are then passed to the partial loader" \
                                 "  --load-mode all:               each processor loads all the data (default, and often fine for zoom simulations)." \
                                 "  --load-mode server-shared-mem: a server process manages the data, passing to other processes via shared memory")
        parser.add_argument('--type', action='store', type=str, dest='htype',
                            help="Secify the object type to run on by tag name (or integer). Can be halo, group, or BH.")
        parser.add_argument('--hmin', action='store', type=int, default=0,
                            help="Do not calculate halos below the specified halo")
        parser.add_argument('--hmax', action='store', type=int,
                            help="Do not calculate halos above the specified halo")
        parser.add_argument('--verbose', action='store_true',
                            help="Allow all output from calculations (by default print statements are suppressed)")
        parser.add_argument('--part', action='store', nargs=2, type=int,
                            metavar=('N', 'M'),
                            help="Emulate MPI by handling slice N out of the total workload of M items. If absent, use real MPI.")
        parser.add_argument('--backend', action='store', type=str,
                            help="Specify the paralellism backend (e.g. pypar, mpi4py)")
        parser.add_argument('--include-only', action='append', type=str,
                            help="Specify a filter that describes which objects the calculation should be executed for. Multiple filters may be specified, in which case they must all evaluate to true for the object to be included.")
        parser.add_argument('--explain-classes', action='store_true',
                            help="Log some explanation for why property classes are selected (when there is any ambiguity)")

    def _create_parser_obj(self):
        parser = argparse.ArgumentParser()
        core.supplement_argparser(parser)
        return parser

    def _build_file_list(self):
        query = core.sim_query_from_name_list(self.options.sims)
        files = []
        if self.options.latest:
            for x in query.all():
                try:
                    files.append(x.timesteps[-1])
                except IndexError:
                    pass
        else:
            timestep_filter = core.timestep.TimeStep.simulation_id.in_([q.id for q in query.all()])
            if self.options.timesteps_matching is not None and len(self.options.timesteps_matching)>0:
                subfilter = core.timestep.TimeStep.extension.like(self.options.timesteps_matching[0])
                for m in self.options.timesteps_matching[1:]:
                    subfilter |= core.timestep.TimeStep.extension.like(m)
                timestep_filter &= subfilter

            files = core.get_default_session().query(core.timestep.TimeStep).filter(timestep_filter). \
                order_by(core.timestep.TimeStep.time_gyr).all()



        if self.options.backwards:
            files = files[::-1]

        if self.options.random:
            random.seed(0)
            random.shuffle(files)

        self._files = files

    @property
    def files(self):
        if not hasattr(self, '_files'):
            self._build_file_list()
        return self._files


    def _get_parallel_timestep_iterator(self):
        if parallel_tasks.backend is None:
            # Go sequentially
            ma_files = self.files
        elif self.options.load_mode is not None and self.options.load_mode.startswith('server'):
            # In the case of loading from a centralised server, each node works on the _same_ timestep --
            # parallelism is then implemented at the halo level
            ma_files = parallel_tasks.synchronized(self.files, allow_resume=not self.options.no_resume,
                                                   resumption_id='parallel-timestep-iterator')
        else:
            # In all other cases, different timesteps are distributed to different nodes
            ma_files = parallel_tasks.distributed(self.files, allow_resume=not self.options.no_resume,
                                                  resumption_id='parallel-timestep-iterator')
        return ma_files

    def _get_parallel_halo_iterator(self, items):
        if self.options.load_mode is not None and self.options.load_mode.startswith('server'):
            # Only in 'server' mode is parallelism undertaken at the halo level. See also
            # _get_parallel_timestep_iterator.

            assert parallel_tasks.backend.size()>1, "Cannot use this load mode outside of a parallel session"

            # First, we need to make a barrier because we can't start writing to the database
            # before all nodes have generated their local work lists
            parallel_tasks.barrier()

            return parallel_tasks.distributed(items, allow_resume=False)
        else:
            return items

    def parse_command_line(self, argv=None):
        parser = self._get_parser_obj()
        self.process_options(parser.parse_args(argv))

    def process_options(self, options):
        self.options = options
        core.process_options(self.options)
        self._compile_inclusion_criterion()

        if self.options.load_mode=='all':
            self.options.load_mode=None

        if self.options.verbose:
            self.redirect.enabled = False



    def _compile_inclusion_criterion(self):
        if self.options.include_only:
            includes = ["("+s+")" for s in self.options.include_only]
            include_only_combined = " & ".join(includes)
            self._include = live_calculation.parser.parse_property_name(include_only_combined)
        else:
            self._include = None

    def _log_once_per_timestep(self, *args):
        if parallel_tasks.backend is None or parallel_tasks.backend.rank()==1 or self.options.load_mode is None:
            logger.info(*args)



    def _build_halo_list(self, db_timestep):
        query = core.halo.SimulationObjectBase.timestep == db_timestep
        if self.options.htype is not None:
            query = sqlalchemy.and_(query, core.halo.SimulationObjectBase.object_typecode
                                    == core.halo.SimulationObjectBase.object_typecode_from_tag(self.options.htype))

        if self.options.hmin is not None:
            query = sqlalchemy.and_(query, core.halo.SimulationObjectBase.halo_number>=self.options.hmin)

        if self.options.hmax is not None:
            query = sqlalchemy.and_(query, core.halo.SimulationObjectBase.halo_number<=self.options.hmax)

        needed_properties = self._required_and_calculated_property_names()

        halo_query = core.get_default_session().query(core.halo.SimulationObjectBase).order_by(core.halo.SimulationObjectBase.halo_number).filter(query)
        if self._include:
            needed_properties.append(self._include)

        logger.debug('Gathering existing properties for all halos in timestep %r',db_timestep)
        halo_query = live_calculation.MultiCalculation(*needed_properties).supplement_halo_query(halo_query)

        halos = halo_query.all()

        if self._include:
            inclusion, = self._include.values(halos)
            if any([not (np.issubdtype(inc_i, np.bool_) or inc_i is None) for inc_i in inclusion]):
                raise ValueError("Specified inclusion criterion does not return a boolean")
            if len(inclusion)!=len(halos):
                raise ValueError("Inclusion criterion did not generate results for all the halos")

            # perform filtering:
            halos = [halo_i for halo_i, include_i in zip(halos, inclusion) if include_i]
            self._log_once_per_timestep("User-specified inclusion criterion excluded %d of %d halos",
                                        len(inclusion) - len(halos), len(inclusion))

        return halos


    def _build_existing_properties(self, db_halo):
        existing_properties = db_halo.all_properties
        need_data = self._required_and_calculated_property_names()
        need_data_ids = [core.get_dict_id(x,None) for x in need_data]

        existing_properties_data = AttributableDict()
        for x in existing_properties:
            if x.name_id in need_data_ids:
                name = need_data[need_data_ids.index(x.name_id)]
                existing_properties_data[name] = x.data_raw

        existing_links = db_halo.all_links
        for x in existing_links:
            if x.relation_id in need_data_ids:
                name = need_data[need_data_ids.index(x.relation_id)]
                existing_properties_data[name] = x.halo_to

        existing_properties_data.halo_number = db_halo.halo_number
        existing_properties_data.NDM = db_halo.NDM
        existing_properties_data.NGas = db_halo.NGas
        existing_properties_data.NStar = db_halo.NStar
        existing_properties_data['halo_number'] = db_halo.halo_number
        existing_properties_data['finder_id'] = db_halo.finder_id
        return existing_properties_data

    def _build_existing_properties_all_halos(self, halos):
        return [self._build_existing_properties(h) for h in halos]


    def _is_commit_needed(self, end_of_timestep, end_of_simulation):
        if len(self._pending_properties)==0:
            return False
        if end_of_simulation:
            return True
        elif end_of_timestep and (time.time() - self._last_commit_time > self._writer_minimum):
            return True
        elif time.time() - self._last_commit_time > self._writer_timeout:
            return True
        else:
            return False


    def _commit_results_if_needed(self, end_of_timestep=False, end_of_simulation=False):

        if self._is_commit_needed(end_of_timestep, end_of_simulation):
            self._commit_results()

            self.tracker.report_to_log_or_server(logger)
            self.timing_monitor.report_to_log_or_server(logger)

    def _commit_results(self):
        insert_list(self._pending_properties)
        self._pending_properties = []
        self._last_commit_time = time.time()

    def _queue_results_for_later_commit(self, db_halo, names, results, existing_properties_data):
        for n, r in zip(names, results):
            if isinstance(r, proxy_object.ProxyObjectBase):
                # TODO: possible optimization here using relative_to_timestep_cache
                r = r.relative_to_timestep_id(self._current_timestep_id).resolve(core.get_default_session())
            if self.options.force or (n not in list(existing_properties_data.keys())):
                existing_properties_data[n] = r
                if self.options.debug:
                    logger.info("Debug mode - not creating property %r for %r with value %r", n, db_halo, r)
                else:
                    self._pending_properties.append((db_halo, n, r))

    def _required_and_calculated_property_names(self):
        needed = []
        for x in self._property_calculator_instances:
            if isinstance(x.names, str):
                needed.extend([x.names])
            else:
                needed.extend([name for name in x.names])
            needed.extend([name for name in x.requires_property()])
        return list(np.unique(needed))

    def _should_load_halo_particles(self):
        return any([x.requires_particle_data for x in self._property_calculator_instances])

    def _must_load_timestep_particles(self):
        return self._should_load_halo_particles() and self.options.load_mode is None

    def _unload_timestep(self):
        self._loaded_halo = None
        self._current_halo_id = None
        with check_deleted(self._loaded_timestep):
            self._loaded_timestep=None
            self._current_timestep_id = None


    def _set_current_timestep(self, db_timestep):
        if self._current_timestep_id == db_timestep.id:
            return

        self._unload_timestep()

        if self._must_load_timestep_particles():
            self._loaded_timestep = db_timestep.load(mode=self.options.load_mode)

        elif self._should_load_halo_particles():
            # Keep a snapshot alive for this timestep, even if should_load_timestep_particles is False,
            # because it might be needed for the iord's if we are in partial-load mode.
            try:
                self._loaded_timestep = db_timestep.load(mode=self.options.load_mode)
            except OSError:
                pass

        if self.options.load_mode is None:
            self._run_preloop(self._loaded_timestep, db_timestep,
                              self._property_calculator_instances, self._existing_properties_all_halos)
        else:
            self._run_preloop(None, db_timestep,
                              self._property_calculator_instances, self._existing_properties_all_halos)

        self._current_timestep_id = db_timestep.id


    def _set_current_halo(self, db_halo):
        with parallel_tasks.lock.SharedLock("insert_list"):
            # don't want this to happen in parallel with a database write -- seems to lazily fetch
            # rows in the background
            self._set_current_timestep(db_halo.timestep)

        if self._loaded_halo_id==db_halo.id:
            return

        self._loaded_halo_id=db_halo.id
        self._loaded_halo = None

        if self._should_load_halo_particles():
            self._loaded_halo  = db_halo.load(mode=self.options.load_mode)

        if self.options.load_mode is not None:
            self._run_preloop(self._loaded_halo, db_halo.timestep,
                              self._property_calculator_instances, self._existing_properties_all_halos)


    def _get_current_halo_specified_region_particles(self, db_halo, region_spec):
        return db_halo.timestep.load_region(region_spec,self.options.load_mode)

    def _get_halo_snapshot_data_if_appropriate(self, db_halo, db_data, property_calculator):

        self._set_current_halo(db_halo)

        if property_calculator.region_specification(db_data) is not None:
            return self._get_current_halo_specified_region_particles(db_halo, property_calculator.region_specification(db_data))
        else:
            return self._loaded_halo


    def _get_standin_property_value(self, property_calculator):
        if isinstance(property_calculator.names,str):
            return None
        num = len(property_calculator.names)
        return [None]*num

    def _get_property_value(self, db_halo, property_calculator, existing_properties):
        if property_calculator.no_proxies():
            db_data = db_halo
        else:
            db_data = existing_properties

        result = self._get_standin_property_value(property_calculator)

        try:
            snapshot_data = self._get_halo_snapshot_data_if_appropriate(db_halo, db_data, property_calculator)
        except OSError:
            logger.warning("Failed to load snapshot data for %r; skipping",db_halo)
            self.tracker.register_loading_error()
            return result

        with self.timing_monitor(property_calculator):
            try:
                with self.redirect:
                    result = property_calculator.calculate(snapshot_data, db_data)
                    self.tracker.register_success()
            except Exception as e:
                self.tracker.register_error()

                if self.tracker.should_log_error(e):
                    logger.info("Uncaught exception %r during property calculation %r applied to %r"%(e, property_calculator, db_halo))
                    exc_data = traceback.format_exc()
                    for line in exc_data.split("\n"):
                        logger.info(line)
                    logger.info("If this error arises again, it will be counted but not individually reported.")

                if self.options.catch:
                    tbtype, value, tb = sys.exc_info()
                    pdb.post_mortem(tb)


        return result


    def _run_preloop(self, f, db_timestep, cinstances, existing_properties_all_halos):
        for x in cinstances:
            try:
                with self.redirect:
                    x.preloop(f, db_timestep)
            except Exception:
                logger.exception(
                    f"Uncaught exception during property preloop {x!r} applied to {db_timestep!r}")
                if self.options.catch:
                    traceback.print_exc()
                    tbtype, value, tb = sys.exc_info()
                    pdb.post_mortem(tb)


    def run_property_calculation(self, db_halo, property_calculator, existing_properties):
        names = property_calculator.names
        if type(names) is str:
            listize = True
            names = [names]
        else:
            listize = False

        if all([name in list(existing_properties.keys()) for name in names]) and not self.options.force:
            self.tracker.register_already_exists()
            return

        if not property_calculator.accept(existing_properties):
            self.tracker.register_missing_prerequisite()
            return

        results = self._get_property_value(db_halo, property_calculator, existing_properties)

        if listize:
            results = [results]

        self._queue_results_for_later_commit(db_halo, names, results, existing_properties)

    def run_halo_calculation(self, db_halo, existing_properties):
        for calculator in self._property_calculator_instances:
            busy = True
            nloops = 0
            while busy is True:
                busy = False
                try:
                    nloops += 1
                    self.run_property_calculation(db_halo, calculator, existing_properties)
                except sqlalchemy.exc.OperationalError:
                    if nloops > 10:
                        raise
                    time.sleep(1)
                    busy = True

        self._commit_results_if_needed()


    def run_timestep_calculation(self, db_timestep):
        self._log_once_per_timestep("Processing %r", db_timestep)
        self._property_calculator_instances = properties.instantiate_classes(db_timestep.simulation,
                                                                             self.options.properties,
                                                                             explain=self.options.explain_classes)
        if self.options.with_prerequisites:
            self._add_prerequisites_to_calculator_instances(db_timestep)

        with parallel_tasks.lock.SharedLock("insert_list"):
            logger.debug("Start halo list query")
            db_halos = self._build_halo_list(db_timestep)
            logger.debug("End halo list query")

        self._existing_properties_all_halos = self._build_existing_properties_all_halos(db_halos)

        self._log_once_per_timestep("Successfully gathered existing properties; calculating halo properties now...")

        self._log_once_per_timestep("  %d halos to consider; %d calculation routines for each of them, resulting in %d properties per halo",
                                    len(db_halos), len(self._property_calculator_instances),
                                    sum([1 if isinstance(x.names, str) else len(x.names) for x in self._property_calculator_instances])
                                    )

        self._log_once_per_timestep("  The property modules are:")
        for x in self._property_calculator_instances:
            x_type = type(x)
            self._log_once_per_timestep(f"    {x_type.__module__}.{x_type.__qualname__}")

        for db_halo, existing_properties in \
                self._get_parallel_halo_iterator(list(zip(db_halos, self._existing_properties_all_halos))):
            self._existing_properties_this_halo = existing_properties
            self.run_halo_calculation(db_halo, existing_properties)

        self._log_once_per_timestep("Done with %r", db_timestep)
        self._unload_timestep()

        self._commit_results_if_needed(end_of_timestep=True)

    def _add_prerequisites_to_calculator_instances(self, db_timestep):
        will_calculate = []
        requirements = []
        for inst in self._property_calculator_instances:
            if isinstance(inst.names, str):
                will_calculate+=[inst.names]
            else:
                will_calculate+=inst.names
            requirements+=inst.requires_property()

        for r in requirements:
            if r not in will_calculate:
                new_instance = properties.instantiate_class(db_timestep.simulation, r)
                self._log_once_per_timestep("Missing prerequisites - added class %r", type(new_instance))
                self._log_once_per_timestep("                        providing properties %r", new_instance.names)
                self._property_calculator_instances = [new_instance]+self._property_calculator_instances
                self._add_prerequisites_to_calculator_instances(db_timestep) # everything has changed; start afresh
                break


    def run_calculation_loop(self):
        # NB both these objects must be created at the same place in all processes,
        # since creating them is a 'barrier'-like operation
        self.timing_monitor = timing_monitor.TimingMonitor(allow_parallel=True)
        self.tracker = CalculationSuccessTracker(allow_parallel=True)

        parallel_tasks.database.synchronize_creator_object()

        self._last_commit_time = time.time()
        self._pending_properties = []

        for f_obj in self._get_parallel_timestep_iterator():
            self.run_timestep_calculation(f_obj)

        self._commit_results_if_needed(True,True)


class CalculationSuccessTracker(accumulative_statistics.StatisticsAccumulatorBase):
    def __init__(self, allow_parallel=False):
        self.reset() # comes before __init__, since the latter stores a copy for use in report_to_log_if_needed
        super().__init__(allow_parallel=allow_parallel)

        self._posted_errors = parallel_tasks.shared_set.SharedSet('posted_errors',allow_parallel)

    def should_log_error(self, exception):
        tb = "\n".join(traceback.format_exception(type(exception), exception, exception.__traceback__))
        return not self._posted_errors.add_if_not_exists(tb)

    def report_to_log(self, logger):
        if sum([self._succeeded,
                self._skipped_error,
                self._skipped_loading_error,
                self._skipped_existing,
                self._skipped_missing_prerequisite]) == 0:
            return
        logger.info("PROPERTY CALCULATION SUMMARY")
        logger.info("            Succeeded: %d property calculations", self._succeeded)
        logger.info("              Errored: %d property calculations", self._skipped_error)
        logger.info("  Errored during load: %d property calculations", self._skipped_loading_error)
        logger.info("       Already exists: %d property calculations", self._skipped_existing)
        logger.info("Missing pre-requisite: %d property calculations", self._skipped_missing_prerequisite)

    def register_error(self):
        self._skipped_error+=1

    def register_loading_error(self):
        self._skipped_loading_error+=1

    def register_success(self):
        self._succeeded+=1

    def register_missing_prerequisite(self):
        self._skipped_missing_prerequisite+=1

    def register_already_exists(self):
        self._skipped_existing+=1

    def reset(self):
        self._succeeded = 0
        self._skipped_error = 0
        self._skipped_loading_error = 0
        self._skipped_existing = 0
        self._skipped_missing_prerequisite = 0

    def add(self, other):
        self._succeeded += other._succeeded
        self._skipped_error += other._skipped_error
        self._skipped_loading_error += other._skipped_loading_error
        self._skipped_existing += other._skipped_existing
        self._skipped_missing_prerequisite += other._skipped_missing_prerequisite

    def __eq__(self, other):
        return type(other) == type(self) and \
               self._succeeded == other._succeeded and \
               self._skipped_error == other._skipped_error and \
               self._skipped_loading_error == other._skipped_loading_error and \
               self._skipped_existing == other._skipped_existing and \
               self._skipped_missing_prerequisite == other._skipped_missing_prerequisite
