from . import HaloProperties, TimeChunkedProperty
import numpy as np
import math
import pynbody
import re
import scipy, scipy.interpolate
import weakref

class BHShortenedLog(object):
    _cache = {}
    
        
    @classmethod
    def get_existing_or_new(cls, f,filename):
        name, stepnum = re.match("^(.*)\.(0[0-9]*)$",filename).groups()
        obj = cls._cache.get(name, None)
        if obj is not None:
            return obj
        
        obj = cls(f,filename)
        cls._cache[name] = obj
        return obj


    def __init__(self, f, filename):
        name, stepnum = re.match("^(.*)\.(0[0-9]*)$",filename).groups()
        ars = [[] for i in range(15)]
        for line in open(name+".shortened.orbit"):
            line_split = line.split()
            ars[0].append(int(line_split[0]))
            for i in range(1,15):
                ars[i].append(float(line_split[i]))


        wrapped_ars = [pynbody.array.SimArray(x) for x in ars]
        for w in wrapped_ars:
            w.sim = f
        #bhid, time, step, mass, x, y, z, vx, vy, vz, pot, mdot, deltaM, E, dtEff, scalefac = wrapped_ars
        bhid, time, step, mass, x, y, z, vx, vy, vz, mdot, mdotmean, mdotsig, scalefac, dM = wrapped_ars
        bhid = np.array(bhid,dtype=int)
        print len(time),"entries"

        bhid[(bhid < 0)] = 2 * 2147483648 + bhid[(bhid < 0)]

        munits = f.infer_original_units("Msol")
        posunits = f.infer_original_units("kpc")
        velunits = f.infer_original_units("km s^-1")
        #potunits = velunits**2
        tunits = posunits/velunits
        #Eunits = munits*potunits
        # decorate with units

        x *= scalefac
        y *= scalefac
        z *= scalefac
        vx *= scalefac
        vy *= scalefac
        vz *= scalefac

        mass.units = munits
        x.units = y.units = z.units = posunits / pynbody.units.Unit('a')
        vx.units = vy.units = vz.units = velunits / pynbody.units.Unit('a')
        #pot.units = potunits
        time.units = tunits
        mdot.units = munits/tunits
        mdotsig.units = munits/tunits
        mdotmean.units = munits/tunits
        dM.units = munits
        #E.units = Eunits


        x.convert_units('kpc')
        y.convert_units('kpc')
        z.convert_units('kpc')
        vx.convert_units('km s^-1')
        vy.convert_units('km s^-1')
        vz.convert_units('km s^-1')
        mdot.convert_units('Msol yr^-1')
        mdotmean.convert_units('Msol yr^-1')
        mdotsig.convert_units('Msol yr^-1')
        mass.convert_units("Msol")
        time.convert_units("Gyr")
        dM.convert_units("Msol")
        #E.convert_units('erg')


        self.vars = {'bhid':bhid, 'step':step, 'x':x, 'y':y, 'z':z,
                    'vx':vx, 'vy':vy, 'vz': vz, 'mdot': mdot, 'mdotmean':mdotmean,'mdotsig':mdotsig, 'mass': mass,
                     'time': time, 'dM': dM}


    def get_at_stepnum(self, stepnum):
        mask = self.vars['step']==stepnum
        return dict((k,v[mask]) for k,v in self.vars.iteritems())

    def get_for_named_snapshot(self, filename):
        name, stepnum = re.match("^(.*)\.(0[0-9]*)$",filename).groups()
        stepnum = int(stepnum)
        return self.get_at_stepnum(stepnum)


class BH(HaloProperties):

    @classmethod
    def name(self):
        return "BH_mdot", "BH_mdot_ave", "BH_mdot_std", "BH_central_offset", "BH_central_distance", "BH_mass"

    def requires_property(self):
        return []

    @classmethod
    def no_proxies(self):
        return True

    def preloop(self, f, filename, pa):
        if f is not None:
            self.log = BHShortenedLog.get_existing_or_new(f,filename)
            self.filename = filename
            print self.log

    def calculate(self, halo, properties):
        import halo_db as db
        if not isinstance(properties, db.Halo):
            raise RuntimeError("No proxies, please")
        boxsize = float(halo.properties['boxsize'].in_units('kpc', a = halo.properties['a']))
        halo = halo.s

        if len(halo)!=1:
            raise RuntimeError("Not a BH!")

        if halo['tform'][0]>0:
            raise RuntimeError("Not a BH!")

        vars = self.log.get_for_named_snapshot(self.filename)

        mask = vars['bhid']==halo['iord']
        if(mask.sum()==0):
            raise RuntimeError("Can't find BH in .orbit file")

        # work out who's the main halo
        main_halo = None
        for i in properties.reverse_links:
            if i.relation.text.startswith("BH"):
                main_halo = i.halo_from
                break
        if main_halo is None:
            raise RuntimeError("Can't relate BH to its parent halo")
        print "Main halo is:", main_halo

        main_halo_ssc = main_halo['SSC']

        entry = np.where(mask)[0]

        print "target entry is",entry
        final = {}
        for t in 'x','y','z','vx','vy','vz','mdot', 'mass', 'mdotmean','mdotsig':
            final[t] = float(vars[t][entry])

        offset = np.array((final['x'],final['y'],final['z']))-main_halo_ssc
        bad, = np.where(np.abs(offset) > boxsize/2.)
        offset[bad] = -1.0 * (offset[bad]/np.abs(offset[bad])) * (boxsize - np.abs(offset[bad]))

        return final['mdot'], final['mdotmean'], final['mdotsig'], offset, np.linalg.norm(offset), final['mass']


class BHAccHistogram(TimeChunkedProperty):
    @classmethod
    def name(self):
        return "BH_mdot_histogram"

    def requires_property(self):
        return []


    def preloop(self, f, filename, pa):
        self.log = BHShortenedLog.get_existing_or_new(f,filename)

    @classmethod
    def no_proxies(self):
        return True

    def calculate(self, halo, properties):

        halo = halo.s

        if len(halo)!=1:
            raise RuntimeError("Not a BH!")

        if halo['tform'][0]>0:
            raise RuntimeError("Not a BH!")

        mask = self.log.vars['bhid']==halo['iord']
        if(mask.sum()==0):
            raise RuntimeError("Can't find BH in .orbit file")

        t_orbit = self.log.vars['time']
        Mdot_orbit = self.log.vars['mdotmean']
        order = np.argsort(t_orbit)

        t_max = properties.timestep.time_gyr
        t_grid = np.linspace(0, self.tmax_Gyr, self.nbins)
        

        Mdot_grid = scipy.interpolate.interp1d(t_orbit[order], Mdot_orbit[order], bounds_error=False)(t_grid)
        

        #print t_max
        #print Mdot_grid
        
        return Mdot_grid[self.store_slice(t_max)]

class BHGalaxy(HaloProperties):
    @classmethod
    def name(self):
        return "massive_BH_mass", "massive_BH_dist", "massive_BH_mdot", "central_BH_mass", "central_BH_dist", "central_BH_mdot", "bright_BH_mass", "bright_BH_dist", "bright_BH_mdot", "massive_BH_iord", "central_BH_iord", "bright_BH_iord"

    def requires_property(self):
        return ['BH']

    @classmethod
    def requires_simdata(self):
        return False

    @classmethod
    def no_proxies(self):
        return True

    def calculate(self, halo, properties):
        bhmass = [bh['BH_mass'] for bh in properties['BH_central']]
        bhiord = [bh.halo_number for bh in properties['BH_central']]
        mdot = [bh['BH_mdot_ave'] for bh in properties['BH_central']]
        offset = [bh['BH_central_distance'] for bh in properties['BH_central']]

        indm = np.argmax(bhmass)
        indo = np.argmin(offset)
        indl = np.argmax(mdot)

        return bhmass[indm], offset[indm], mdot[indm], bhmass[indo], offset[indo], mdot[indo], bhmass[indl], offset[indl], mdot[indl], bhiord[indm], bhiord[indo], bhiord[indl]

class BHHostProperties(HaloProperties):
    def __init__(self, propname):
        self._host_prop = propname

    @classmethod
    def name(cls):
        return "bh_host"

    @classmethod
    def requires_simdata(self):
        return False

    def calculate(self, halo, properties):
        return properties.host_halo[self._host_prop]
