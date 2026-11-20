# Documenting newr code

Doc comments are frozen in M6 (SPEC.md section 6, section 24.2 idea
14). A doc comment is a `///` line at the start of a logical line,
and it documents the declaration or statement directly below it:

```newr
/// Doubles a number.
fn double(x: int) -> int:
  return x * 2

print double(21)
```

A doc comment is syntax only: it changes no checking, no name
resolution, and no emitted code. The same program with and without
its doc comments builds to byte-identical output.

## Rules

- A doc comment sits directly above its item. A blank line between
  the comment and the item breaks the attachment: the comment then
  documents nothing.
- Several `///` lines in a row join into one doc, lines separated
  by newlines.
- A `///` line at the very top of a file, followed by a blank line,
  is the file header: it documents the file, not the first item.
- `///` after code on the same line is not a doc comment. `let x
  = 12 /// 3` stays three division operators, exactly as before.
- Ordinary `#` comments are unchanged: they are never doc comments.

## Generating the docs

`newr docs` generates the documentation for the checkout:

```sh
newr docs              # writes docs/api.md and docs/examples.md
newr docs --out build  # write them somewhere else instead
```

`docs/api.md` is the stdlib API reference (SPEC.md section 12),
generated from `compiler/modules.py`, the single source of truth
for signatures. Every stdlib member carries a `doc` string there;
the stdlib has no newr source declarations to hang a `///` comment
on, so the `doc` field is the member's doc comment, stored with
its signature.

`docs/examples.md` is one section per program in `examples/`,
from the `///` comments in the source. Both files are
deterministic: running the command twice produces identical bytes.

## A second sample

```newr
/// Prints the area of a circle with radius r.
fn circle_area(r: float) -> float:
  return math.pi * r * r

print circle_area(1.0)
```
