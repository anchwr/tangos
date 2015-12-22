# Environment setup - bash version
#
# DO NOT edit this file
#
# Instead, create your own file called environment_local.sh, and
# export any environment variables you wish to change there. The end
# of this script sources environment_local.sh for you.

RELATIVEDIR=$( dirname ${BASH_SOURCE:-$0} )
DIR=$( cd $RELATIVEDIR && pwd )
export PYTHONPATH=$DIR/modules/:$PYTHONPATH
export PATH=$DIR/tools/:$PATH

if [ -z "$HALODB_ROOT" ]; then
  export HALODB_ROOT=$DIR/../db_galaxies/
fi

if [ -z "$HALODB_DEFAULT_DB" ]; then
    export HALODB_DEFAULT_DB=$DIR/data.db
fi

if [[ -e environment_local.sh ]]
then
    source environment_local.sh
fi
