"""Apply names.toml to the symbol files and to code that uses the old names.

Usage (from the repository that contains Quest64Syms):
    python Quest64Syms/tools/apply_names.py --code us.rev0.toml patches
    python Quest64Syms/tools/apply_names.py --code us.rev0.toml patches --check
    python Quest64Syms/tools/apply_names.py --export-decomp

--code      Files or folders whose references should be renamed (C, headers, TOML, linker scripts).
--check     Change nothing; exit with status 1 if the symbol files or code are out of date.
--export-decomp
            Print the names as symbol_addrs.txt lines, for upstreaming to the decomp project.
"""
import argparse
import pathlib
import re
import sys
import tomllib

SYMS_DIR = pathlib.Path(__file__).resolve().parent.parent
CODE_SUFFIXES = {".c", ".h", ".cpp", ".hpp", ".inc", ".toml", ".ld"}
SKIP_DIRS = {".git", "build", "RecompiledFuncs", "RecompiledPatches"}
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SNAKE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
# Default names the tools generate, e.g. func_80014890 or D_8008C5E8.
DEFAULT_NAME_RE = re.compile(r"\b(?:func|D)_([0-9A-Fa-f]{8})\b")

# kind -> (names.toml table, symbol file)
KINDS = {"function": "dump.toml", "data": "data_dump.toml"}


def read_text(path):
    # newline="" keeps CRLF line endings as-is, so files are written back unchanged apart from renames.
    with open(path, encoding="utf-8", errors="surrogateescape", newline="") as f:
        return f.read()


def write_text(path, text):
    with open(path, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
        f.write(text)


def entry_re(vram):
    return re.compile(r'(\{ name = ")([^"]+)(", vram = 0x%08X\b)' % vram, re.IGNORECASE)


def load_registry(errors):
    registry = tomllib.loads((SYMS_DIR / "names.toml").read_text(encoding="utf-8"))
    entries = []
    seen_names, seen_vrams = {}, {}
    for kind in KINDS:
        for item in registry.get(kind, []):
            vram, name = item.get("vram"), item.get("name", "")
            where = f"{kind} {vram:#010x}" if isinstance(vram, int) else f"{kind} {name!r}"
            if not isinstance(vram, int):
                errors.append(f"{where}: missing vram")
                continue
            if not NAME_RE.match(name):
                errors.append(f"{where}: {name!r} isn't a valid C identifier")
                continue
            if not SNAKE_RE.match(name) and kind == "function":
                errors.append(f"{where}: {name!r} isn't snake_case")
            if DEFAULT_NAME_RE.fullmatch(name):
                errors.append(f"{where}: {name!r} is a placeholder name; leave it out of names.toml instead")
            if name in seen_names:
                errors.append(f"{where}: {name!r} is also used for {seen_names[name]}")
            if vram in seen_vrams:
                errors.append(f"{where}: address listed twice")
            seen_names[name] = where
            seen_vrams[vram] = where
            entries.append((kind, vram, name))
    return entries


def plan_symbol_renames(entries, errors):
    """Returns {kind: {old_name: new_name}} for entries whose symbol file name differs."""
    renames = {kind: {} for kind in KINDS}
    texts = {kind: read_text(SYMS_DIR / file) for kind, file in KINDS.items()}
    existing = {kind: set(re.findall(r'\{ name = "([^"]+)"', text)) for kind, text in texts.items()}
    for kind, vram, name in entries:
        matches = entry_re(vram).findall(texts[kind])
        if len(matches) != 1:
            errors.append(f"{kind} {vram:#010x}: found {len(matches)} entries in {KINDS[kind]}, expected 1")
            continue
        old = matches[0][1]
        if old != name:
            if name in existing[kind]:
                errors.append(f"{kind} {vram:#010x}: {name!r} is already the name of another symbol")
            renames[kind][old] = name
    return renames


def apply_symbol_renames(entries, renames):
    for kind, file in KINDS.items():
        if not renames[kind]:
            continue
        path = SYMS_DIR / file
        text = read_text(path)
        for k, vram, name in entries:
            if k == kind:
                text = entry_re(vram).sub(lambda m: m.group(1) + name + m.group(3), text)
        write_text(path, text)


def code_files(paths):
    for root in paths:
        root = pathlib.Path(root)
        if root.is_file():
            yield root
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.suffix in CODE_SUFFIXES and not SKIP_DIRS & set(path.parts):
                yield path


def update_code(paths, entries, renames, write):
    """Renames old names in code. Returns a list of (file, old, new, count) still to change."""
    old_to_new = {old: new for kind in renames.values() for old, new in kind.items()}
    named = {vram: name for _, vram, name in entries}
    # Also catch default names whose address is now named, even if the symbol file was updated earlier.
    pattern = re.compile(r"\b(" + "|".join(map(re.escape, old_to_new)) + r")\b") if old_to_new else None
    pending = []
    for path in code_files(paths):
        if path.resolve().parent == SYMS_DIR and path.name in KINDS.values():
            continue
        text = read_text(path)
        new_text = text
        if pattern:
            new_text = pattern.sub(lambda m: old_to_new[m.group(1)], new_text)

        def default_name(m):
            new = named.get(int(m.group(1), 16))
            return new if new is not None else m.group(0)
        new_text = DEFAULT_NAME_RE.sub(default_name, new_text)

        if new_text != text:
            changed = sorted(set(re.findall(r"\b\w+\b", text)) - set(re.findall(r"\b\w+\b", new_text)))
            pending.append((path, changed))
            if write:
                write_text(path, new_text)
    return pending


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--code", nargs="*", default=[], help="files or folders to update")
    parser.add_argument("--check", action="store_true", help="change nothing; fail if anything is out of date")
    parser.add_argument("--export-decomp", action="store_true", help="print names as symbol_addrs.txt lines")
    args = parser.parse_args()

    errors = []
    entries = load_registry(errors)
    renames = plan_symbol_renames(entries, errors)
    if errors:
        print("names.toml has problems:", *errors, sep="\n  ", file=sys.stderr)
        return 1

    if args.export_decomp:
        for kind, vram, name in sorted(entries, key=lambda e: e[1]):
            suffix = " // type:func" if kind == "function" else ""
            print(f"{name} = 0x{vram:08X};{suffix}")
        return 0

    symbol_changes = [(old, new) for kind in renames.values() for old, new in kind.items()]
    code_changes = update_code(args.code, entries, renames, write=not args.check)
    if not args.check:
        apply_symbol_renames(entries, renames)

    verb = "would rename" if args.check else "renamed"
    for old, new in symbol_changes:
        print(f"symbols: {verb} {old} -> {new}")
    for path, old_names in code_changes:
        print(f"code: {verb} {', '.join(old_names)} in {path}")
    if not symbol_changes and not code_changes:
        print(f"Up to date ({len(entries)} names).")
    elif not args.check:
        print("Done. Regenerate recompiled code (build-windows.bat does this when the symbols change).")
    return 1 if args.check and (symbol_changes or code_changes) else 0


if __name__ == "__main__":
    sys.exit(main())
