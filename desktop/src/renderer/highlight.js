'use strict';

/**
 * The viewer's syntax colours (design 1p «Código y texto»: line numbers, highlighting, the
 * task's change marks). The canvas tokenizes every file one way (`_fxTok`: line comment,
 * quoted string, number, identifier, the rest) with one set of colours; this keeps those
 * colours and that order, and adds what a file in another language needs to read right:
 * its own keywords and comment marks, and block comments that run across lines.
 *
 * `tokenize(text, ext)` returns one array of `{t, c}` per line (`t` the text, `c` a class
 * from `CLASS`), so the viewer never parses anything itself and never builds HTML from it.
 */

/** Token classes and their colours, as in the canvas. Each is a CSS variable (explorer.css
 * sets its light and dark value), so a line already drawn follows a theme change. */
const COLOR = {
  comment: 'var(--fx-hl-comment)',
  string: 'var(--fx-hl-string)',
  number: 'var(--fx-hl-number)',
  keyword: 'var(--fx-hl-keyword)',
  type: 'var(--fx-hl-type)',
  name: 'var(--fx-hl-name)',
  punct: 'var(--fx-hl-punct)',
};

const JS = 'import from export interface function const let var return if else for while do switch case break continue default throw try catch finally new class extends super this typeof instanceof in of void delete yield async await type enum implements private public protected readonly static abstract declare namespace module as true false null undefined test expect describe it';
const PY = 'def class return if elif else for while break continue pass import from as with try except finally raise lambda yield global nonlocal assert del in is not and or async await True False None self';
const GO = 'package import func return if else for range switch case default break continue go defer select chan map struct interface type var const fallthrough goto nil true false';
const RS = 'fn let mut const static struct enum impl trait pub crate mod use self super where for in loop while if else match return break continue as dyn move ref unsafe async await true false Some None Ok Err';
const C_LIKE = 'class interface extends implements public private protected static final abstract void return if else for while do switch case break continue default new try catch finally throw throws this super import package int long short byte char float double boolean bool true false null nullptr const struct enum typedef sizeof include define namespace using template typename virtual override auto unsigned signed';
const RB = 'def class module end if elsif else unless while until for in do return yield begin rescue ensure raise then case when self nil true false and or not require';
const PHP = 'function class interface extends implements public private protected static return if else elseif foreach for while do switch case break continue default new try catch finally throw echo namespace use const true false null array fn match';
const SH = 'if then else elif fi for in do done while until case esac function return export local readonly echo exit set unset source';
const SQL = 'select from where insert into values update set delete create table index view drop alter add column primary key foreign references not null default and or in is like join left right inner outer on group by order having limit offset as distinct union all case when then else end begin commit rollback';

/** Extension -> `{kw, line, block, hash}`: keywords, line-comment marks, block-comment pairs. */
const LANGS = (() => {
  const set = (words) => new Set(words.split(' '));
  const c = { line: ['//'], block: [['/*', '*/']] };
  const table = {};
  const add = (exts, lang) => exts.split(' ').forEach((ext) => (table[ext] = lang));
  add('ts tsx js jsx mjs cjs json jsonc', { kw: set(JS), ...c });
  add('py', { kw: set(PY), line: ['#'], block: [] });
  add('go', { kw: set(GO), ...c });
  add('rs', { kw: set(RS), ...c });
  add('java c cpp cc h hpp cs kt swift', { kw: set(C_LIKE), ...c });
  add('rb', { kw: set(RB), line: ['#'], block: [] });
  add('php', { kw: set(PHP), line: ['//', '#'], block: [['/*', '*/']] });
  add('sh bash zsh', { kw: set(SH), line: ['#'], block: [] });
  add('sql', { kw: set(SQL), line: ['--'], block: [['/*', '*/']], caseless: true });
  add('css scss', { kw: new Set(), line: [], block: [['/*', '*/']] });
  add('html xml svg', { kw: new Set(), line: [], block: [['<!--', '-->']] });
  add('yaml yml toml ini cfg conf', { kw: set('true false null yes no on off'), line: ['#', ';'], block: [] });
  return table;
})();

const PLAIN = { kw: new Set(), line: [], block: [] };

function langOf(ext) {
  return LANGS[String(ext || '').toLowerCase()] || PLAIN;
}

/** One line's tokens, with `state.block` the block comment still open from the line
 * before (its closing mark) or null. */
function tokenizeLine(line, lang, state) {
  const out = [];
  let i = 0;
  const push = (t, c) => {
    if (!t) return;
    const last = out[out.length - 1];
    if (last && last.c === c) last.t += t;
    else out.push({ t, c });
  };
  while (i < line.length) {
    if (state.block) {
      const end = line.indexOf(state.block, i);
      if (end < 0) {
        push(line.slice(i), 'comment');
        return out;
      }
      push(line.slice(i, end + state.block.length), 'comment');
      i = end + state.block.length;
      state.block = null;
      continue;
    }
    const rest = line.slice(i);
    const opening = lang.block.find(([open]) => rest.startsWith(open));
    if (opening) {
      state.block = opening[1];
      push(opening[0], 'comment');
      i += opening[0].length;
      continue;
    }
    if (lang.line.some((mark) => rest.startsWith(mark))) {
      push(rest, 'comment');
      return out;
    }
    const quote = rest[0];
    if (quote === '"' || quote === "'" || quote === '`') {
      let j = 1;
      while (j < rest.length && rest[j] !== quote) j += rest[j] === '\\' ? 2 : 1;
      push(rest.slice(0, j + 1), 'string');
      i += j + 1;
      continue;
    }
    const number = /^\d+(?:\.\d+)?/.exec(rest);
    if (number && !/[\w$]/.test(line[i - 1] || '')) {
      push(number[0], 'number');
      i += number[0].length;
      continue;
    }
    const word = /^[A-Za-z_$][\w$]*/.exec(rest);
    if (word) {
      const w = word[0];
      const keyword = lang.kw.has(lang.caseless ? w.toLowerCase() : w);
      push(w, keyword ? 'keyword' : /^[A-Z]/.test(w) ? 'type' : 'name');
      i += w.length;
      continue;
    }
    const space = /^\s+/.exec(rest);
    if (space) {
      push(space[0], 'punct');
      i += space[0].length;
      continue;
    }
    push(rest[0], 'punct');
    i += 1;
  }
  return out;
}

/** `text` split into lines of tokens for a file with extension `ext`. */
function tokenize(text, ext) {
  const lang = langOf(ext);
  const state = { block: null };
  return String(text).split(/\r?\n/).map((line) => tokenizeLine(line, lang, state));
}

export { tokenize, COLOR, langOf };
