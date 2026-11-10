# newr error index

Every diagnostic code the compiler can emit (SPEC.md §16). `newr --explain
<CODE>` prints the long form of any code below. A test
(`tests/test_eval_fixes.py`, `TestErrorIndex`) enforces that the codes in
this file are exactly the keys of `EXPLAIN` in `compiler/cli.py` — if you
add a code, add it in both places.

Numbered codes follow the rustc-style families: `E1xxx` syntax, `E2xxx`
types, `E3xxx` resolver, `W1xxx` warnings. The lexer has its own family
of descriptive `E_*` codes instead of numbers: the lexer runs before any
numbered stage exists, and a descriptive name (`E_BAD_INDENT`) is clearer
at the point of failure than a number would be. That is a deliberate
exception to the numbering scheme, not an oversight.

## Syntax — `E1xxx`

| Code | Meaning |
| ---- | ------- |
| `E1001` | Syntax error (also: "too deeply nested" past 200 levels, M-01) |

## Types — `E2xxx`

| Code | Meaning |
| ---- | ------- |
| `E2001` | Mismatched types |
| `E2002` | Type annotation needed (empty list/dict/set) |
| `E2003` | Heterogeneous list/dict literal |
| `E2004` | Non-`bool` condition |
| `E2005` | Use before definition |
| `E2006` | Non-exhaustive match |
| `E2007` | `break`/`continue`/`return` in the wrong place |
| `E2008` | Discord rule violation |
| `E2009` | i18n violation (`strings`/`t()`) |
| `E2010` | Not all paths answer (command handler) |
| `E2011` | db module rule violation |
| `E2012` | http module rule violation |
| `E2013` | json module rule violation |
| `E2014` | ui module rule violation |

## Resolver — `E3xxx`

| Code | Meaning |
| ---- | ------- |
| `E3001` | Undefined name (also: unknown type, module has no member) |
| `E3002` | Wrong number of arguments |
| `E3003` | Duplicate definition |
| `E3004` | Import cycle |
| `E3005` | Name not exported (`pub`) |
| `E3006` | Bot/command/event/task/top-level statement in an imported file |
| `E3007` | Reserved name shadowed |
| `E3008` | Unknown module / missing file |

## Warnings — `W1xxx`

Warnings print but never fail the build.

| Code | Meaning |
| ---- | ------- |
| `W1001` | Unused variable / parameter / option |
| `W1002` | Unused import |
| `W1003` | Unreachable code |
| `W1004` | Shadowing |
| `W1005` | Always-true/false literal condition |
| `W1006` | Unbounded loop in a handler body |
| `W1007` | Extra `t()` argument |
| `W1008` | Integer division in a float context |
| `W1009` | Reply length cannot be verified |
| `W1010` | Privileged intent declared |

## Lexer — `E_*` family

| Code | Meaning |
| ---- | ------- |
| `E_BAD_ESCAPE` | Unknown or malformed string escape |
| `E_BAD_INDENT` | Wrong indentation (not exactly 2 spaces per level, or a dedent to no known level) |
| `E_BAD_LINE_ENDING` | Lone carriage return (CR) — lines end with LF or CRLF |
| `E_BAD_NUMBER` | Malformed number literal (`0x`/`0b`/`0o` with bad or missing digits; misplaced `_`) |
| `E_INT_OVERFLOW` | Integer literal outside the 64-bit signed range |
| `E_STRAY_BRACE` | Stray `}` in a string (write `}}` for a literal `}`) |
| `E_TAB_INDENT` | Tab indentation (use 2 spaces per level) |
| `E_UNEXPECTED_CHAR` | Character that is not part of the language |
| `E_UNTERMINATED_STRING` | String never closed |
