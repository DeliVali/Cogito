---
name: clean-dead-code
description: Use when the person asks to remove dead code (unused functions, classes, imports or files) or to clean up a codebase. Quita código muerto (funciones, métodos o imports sin usar, que no se usan, que no se utilizan, que sobran o que nadie usa) para limpiar.
---

# Removing dead code

Dead code is code nothing runs: a function nobody calls, an import nobody reads, a file
nobody imports. A tool's list is only a list of **candidates**. Remove a name only after
you have checked it yourself (step 2). When unsure, keep it and say why.

Work only in the folder the person named, or the whole project if they named none.

## 1. Find candidates

Use the language's own tool. Run it through `shell` from the project root. The commands
below fetch the tool for this run only (`uvx`, `npx`, `go run`); never install anything
globally. If a command fails (no network, no toolchain), go to "No tool" below.

**Python**
- `uvx vulture PATH --min-confidence 60 --exclude .venv,venv,node_modules,build,dist,.tox`
  lists unused functions, classes, methods, variables (60% confidence), imports (90%) and
  unreachable code or arguments (100%), leaving out installed and built code. 60% is a
  guess: check every 60% line by hand. Exit code 3 only means it found something.
- `uvx ruff check --select F401,F841 --output-format concise PATH`: unused imports (F401)
  and unused local variables (F841). Use `ruff` instead of `uvx ruff` if it is installed.

**JavaScript / TypeScript**
- `npx --yes knip` lists unused files, unused exports and unused dependencies, starting
  from the project's entry points. `pnpm dlx knip` or `bunx knip` if the project uses them.
- TypeScript, when `typescript` is in the project's `package.json`:
  `npx tsc --noEmit --noUnusedLocals --noUnusedParameters -p .` (error TS6133: declared
  but never read). Do not pass file names: then `tsconfig.json` is ignored.

**Go**
- `go run honnef.co/go/tools/cmd/staticcheck@latest ./...` and read the lines ending in
  `(U1000)`: unused functions, types, variables and constants.
- `go run golang.org/x/tools/cmd/deadcode@latest ./...` lists functions no `main` can
  reach (add `-test` to count tests as callers). It also lists exported functions; in a
  library those may be used by other modules, so treat them as kept unless the person
  says otherwise.

**Rust**
- `cargo check --all-targets --message-format short` and read the warnings
  `function ... is never used`, `unused import` and `unused variable`. Rust warns only
  about items that are not exported (`pub`).

**No tool** (another language, or the tool cannot run): list the definitions with `grep`
(for example `^\s*def `, `function `, `func `), then count each name's uses as in step 2.
A name found only on its own definition line is a candidate.

Write the candidates down as a short list: file, line, name. Start with a few, not all.

## 2. Check every candidate

For each name, search the **whole project**, every file type, not only the language's
files: `grep` with pattern `\bNAME\b` and no `glob`. Ignore the definition itself. Look for:

- calls, imports, and the name inside a string: `getattr(obj, "NAME")`, `globals()`,
  `importlib`, reflection, a dict or registry of names, an event or signal name;
- decorators and framework hooks: routes (`@app.route`), CLI commands, fixtures,
  signal handlers, test functions, `__init__`/`__str__` and other dunder methods,
  methods that override a parent class or implement an interface;
- entry points: `pyproject.toml` (`[project.scripts]`, plugin entry points),
  `package.json` (`main`, `exports`, `bin`, `scripts`), `Cargo.toml`, `setup.cfg`,
  Makefiles, Dockerfiles, CI files;
- the public interface: `__all__`, a package's `__init__.py`, an `index.ts`/`index.js`
  that re-exports it, a Go exported name, a Rust `pub` item;
- tests, configuration files, templates (HTML, Jinja), SQL and documentation that show
  how to call it.

Keep it, and note why, when any of these is true:
- something above uses it;
- the project is a library others install, and the name is part of what it exports;
- it is reached by name from outside the repository (a plugin, a URL route, a config key);
- you cannot tell.

## 3. Remove

- One candidate at a time. Use `edit` to delete the function, class or variable.
- Then delete the imports that only it needed (run the step 1 tool again to find them).
- Delete a whole file only when nothing in it is used and nothing imports it. Delete it
  by name: `rm path/to/file.py`. Never `rm -r` a folder. Cogito asks the person before a
  deletion (or allows it in automatic mode) and moves the file to the trash.
- Never touch files outside the project, generated files, or vendored code
  (`node_modules`, `vendor/`, `.venv`).

## 4. Check it still works

After each removal, or each small group, run what the project already has:
its tests (`pytest`, `npm test`, `go test ./...`, `cargo test`), its build or type check
(`tsc --noEmit`, `go build ./...`, `cargo check`). If something fails because of the
removal, put that code back with `edit` and mark the name as kept.

If the project has no tests, at least make sure it still compiles or imports:
`python3 -m compileall -q PATH`, `go vet ./...`, `cargo check`, or `npx tsc --noEmit -p .`
when `typescript` is in `package.json` (without it, `npx tsc` fetches a deprecated 2.0 package).

## 5. Report

Tell the person, briefly:
- **Removed:** file and name of each function, class, import or file.
- **Kept:** each candidate you left, and why (used by name, public interface, unsure).
- **Checked with:** the commands you ran and whether they passed.

Do not commit. If the person wants a commit, they will ask.
