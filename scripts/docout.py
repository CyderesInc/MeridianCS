"""Output-path resolution shared by the three documentation generators.

Snyk flags `sys.argv[1]` reaching `open`/`os.replace`/`os.remove` in each generator as Path
Traversal (18 LOW findings across four scripts). Traversal is the wrong frame: there is no trust
boundary here -- the operator running the script names the file, on their own machine, with their
own privileges. Confining the path to the repo root would fix nothing and break rendering a draft
to a scratchpad.

The *real* bug the pattern was sitting on is destructive: nothing checked what `argv[1]` pointed
at, so `python scripts/make-brief.py SKILL.md` silently replaced SKILL.md with a 134KB PDF --
Chrome's `--print-to-pdf` overwrites without asking and the exit status stayed 0. Requiring the
`.pdf` suffix is what actually stops that, because every real invocation already ends in `.pdf`
and no source file in the tree does.
"""

import os
import sys


def resolve_out(default):
    """Absolute output path from argv[1], or `default`. Exits with a specific message on refusal.

    Guards, in the order a mistake tends to arrive: a directory (forgot the filename), a missing
    parent (typo'd a directory), a non-.pdf target (aimed at a source file). The suffix check is
    the load-bearing one -- the other two only turn an opaque traceback into a sentence.
    """
    out = sys.argv[1] if len(sys.argv) > 1 else default
    out = os.path.abspath(out)

    if os.path.isdir(out):
        _refuse("%s is a directory; pass the full path to the .pdf file to write." % out)

    parent = os.path.dirname(out)
    if not os.path.isdir(parent):
        _refuse("no such directory: %s -- create it, or check the path for a typo." % parent)

    if os.path.splitext(out)[1].lower() != ".pdf":
        _refuse("refusing to write %s: the output must end in .pdf.\n"
                "This generator overwrites its target without asking (and writes a .html "
                "fallback beside it), so an argument aimed at a source file would destroy it."
                % out)

    return out


def _refuse(message):
    sys.stderr.write("error: %s\n" % message)
    sys.exit(2)
