// compare with "google-chrome deps/v8/benchmarks/run.html"
var fs = require('fs');
var path = require('path');
var vm = require('vm');

var dir = path.join(__dirname, '..', '..', 'deps', 'v8', 'benchmarks');

global.print = function(s) {
  if (s === '----') return;
  console.log('misc/v8_bench.js %s', s);
};

global.load = function (x) {
  var srcPath = path.join(dir, x);
  console.log(srcPath);
  var source = fs.readFileSync(srcPath, 'utf8');
  vm.runInThisContext(source, x);
}

load('run.js');
