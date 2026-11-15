// Runs the Python clock code of modm_data.clocks (copied to py/clocks by site/export.py in modm-data) in Pyodide.
// Messages are {id, op, args} in and {id, out} or {id, error} out, everything as JSON.
importScripts("https://cdn.jsdelivr.net/pyodide/v0.26.4/full/pyodide.js");

const FILES = ["__init__.py", "evaluate.py", "solver.py", "configure.py", "modm.py"];

const ready = (async () => {
  const py = await loadPyodide();
  // modm.py renders the SystemClock struct with a jinja2 template
  await py.loadPackage("jinja2");
  py.FS.mkdirTree("/home/pyodide/clocks");
  for (const f of [...FILES.map((f) => "clocks/" + f), "site_api.py"]) {
    const text = await (await fetch(`py/${f}`, {cache: "no-store"})).text();
    py.FS.writeFile(`/home/pyodide/${f}`, text);
  }
  py.runPython("import sys; sys.path.insert(0, '/home/pyodide'); import site_api");
  return py.pyimport("site_api").handle;
})();

onmessage = async ({data: {id, op, args}}) => {
  try {
    const handle = await ready;
    postMessage({id, out: JSON.parse(handle(op, JSON.stringify(args)))});
  } catch (error) {
    // A Python exception: its last line is the message
    const text = String(error.message ?? error).trim().split("\n").pop();
    postMessage({id, error: text});
  }
};
