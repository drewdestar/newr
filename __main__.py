"""zipapp entry point: `python3 -m zipapp . -o newr.pyz` then
`python3 newr.pyz check <file>`. Same entry point as the `newr`
launcher: ``compiler.cli:main``.
"""

from compiler.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
