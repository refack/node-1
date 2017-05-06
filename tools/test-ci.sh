#!/bin/bash
cd `dirname $0`/.. > /dev/null
if [ -z ${PYTHON+x} ]; then export PYTHON=`which python`; fi
${PYTHON} tools/test.py -J --mode=release $*
