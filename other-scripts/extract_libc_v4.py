import os
import subprocess
import shutil
import re
import sys

# Contrib dirs referenced by libc that bmake's default scan skips
# (gated behind options like FLOAT, or header-only on a default build).
# Copied unconditionally, like lib/libc and include.
LIBC_CONTRIB_DIRS = [
    "contrib/gdtoa",
    "contrib/libc-pwcache",
    "contrib/libc-vis",
    "contrib/tzcode",
]

# Per-tree exclusions, skipped during the unconditional copy.
# tzcode ships date/zdump/zic CLI tools that don't belong in libc.
CONTRIB_EXCLUDE = {
    "contrib/tzcode": {
        "date.c", "date.1",
        "zdump.c", "zdump.8",
        "zic.c", "zic.8",
        "Makefile",
        "tzselect.ksh", "tzselect.8",
        "workman.sh",
        "theory.html", "tz-art.html", "tz-how-to.html", "tz-link.html",
        "CONTRIBUTING", "NEWS", "README", "SECURITY",
        "calendars", "version",
    },
}

def run_command(cmd, cwd=None, warn=True):
    """Run a shell command and return (returncode, stdout).

    Nonzero exits are reported on stderr unless warn=False.
    """
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=cwd)
    if result.returncode != 0 and warn:
        sys.stderr.write(f"[warn] command failed (rc={result.returncode}): {cmd}\n")
        for line in result.stderr.strip().splitlines()[:5]:
            sys.stderr.write(f"        {line}\n")
    return result.returncode, result.stdout.strip()

def extract_libc(src_root, output_dir, arches=["amd64", "aarch64"]):
    src_root = os.path.abspath(src_root)
    output_dir = os.path.abspath(output_dir)
    libc_dir = os.path.join(src_root, "lib/libc")
    inc_dir = os.path.join(src_root, "include")
    mk_dir = os.path.join(src_root, "share/mk")

    print(f"Analyzing FreeBSD source at {src_root}...")

    # Extraction overwrites copied files but never deletes. Warn on a
    # non-empty output dir so stale files from a prior run can be cleared.
    if os.path.isdir(output_dir) and any(os.scandir(output_dir)):
        print(f"[warn] output dir {output_dir} is non-empty: extraction overwrites "
              f"but does not delete. Stale/orphaned files from a prior extraction "
              f"will remain. Clear it manually for a clean run.")

    all_files = set()

    # 1. Unconditionally include all of lib/libc, include, share/mk, and the
    #    libc-relevant contrib subtrees.
    print("Collecting all files from lib/libc, include, share/mk, and contrib/{gdtoa,libc-pwcache,libc-vis,tzcode}...")
    unconditional_roots = [(libc_dir, None), (inc_dir, None), (mk_dir, None)] + [
        (os.path.join(src_root, d), CONTRIB_EXCLUDE.get(d)) for d in LIBC_CONTRIB_DIRS
    ]
    for root_dir, exclude in unconditional_roots:
        if not os.path.exists(root_dir):
            print(f"Warning: {root_dir} does not exist.")
            continue
        for root, dirs, files in os.walk(root_dir):
            for f in files:
                if exclude and f in exclude:
                    continue
                all_files.add(os.path.join(root, f))

    # 2. Use arch-specific analysis ONLY to find dependencies in other dirs (like sys/)
    for arch in arches:
        print(f"\nProcessing dependencies for architecture: {arch}")
        # Get SRCS and .PATH to find what is actually used to build libc on this arch
        _, srcs_raw = run_command(f"bmake -m {mk_dir} MACHINE_ARCH={arch} SRCTOP={src_root} -V SRCS", cwd=libc_dir)
        _, path_raw = run_command(f"bmake -m {mk_dir} MACHINE_ARCH={arch} SRCTOP={src_root} -V .PATH", cwd=libc_dir)
        _, cflags = run_command(f"bmake -m {mk_dir} MACHINE_ARCH={arch} SRCTOP={src_root} -V CFLAGS", cwd=libc_dir)

        if not srcs_raw or not path_raw or not cflags:
            print(f"Skipping {arch} due to missing build info.")
            continue

        srcs = srcs_raw.split()
        paths = path_raw.split()

        resolved_srcs = []
        for s in srcs:
            for p in paths:
                if not os.path.isabs(p): p = os.path.abspath(os.path.join(libc_dir, p))
                full_path = os.path.join(p, s)
                if os.path.exists(full_path):
                    resolved_srcs.append(os.path.abspath(full_path))
                    break

        # Also scan the contrib .c files for sys/ and contrib/ headers the
        # bmake walk wouldn't have visited. Honor CONTRIB_EXCLUDE so excluded
        # files aren't re-added (gcc -M lists its input as the first dep).
        for d in LIBC_CONTRIB_DIRS:
            contrib_path = os.path.join(src_root, d)
            if not os.path.exists(contrib_path):
                continue
            exclude = CONTRIB_EXCLUDE.get(d) or set()
            for root, dirs, files in os.walk(contrib_path):
                for f in files:
                    if f in exclude:
                        continue
                    if f.endswith(('.c', '.S', '.s')):
                        resolved_srcs.append(os.path.join(root, f))

        # Arch-specific include symlinks for dependency scanning
        tmp_inc = os.path.join(os.getcwd(), f"tmp_inc_{arch}")
        if os.path.exists(tmp_inc): shutil.rmtree(tmp_inc)
        os.makedirs(tmp_inc)
        mach_dir = "arm64" if arch == "aarch64" else arch

        mach_inc = os.path.join(src_root, f"sys/{mach_dir}/include")
        x86_inc = os.path.join(src_root, "sys/x86/include")

        if os.path.exists(mach_inc):
            os.symlink(mach_inc, os.path.join(tmp_inc, "machine"))
        if os.path.exists(x86_inc):
            os.symlink(x86_inc, os.path.join(tmp_inc, "x86"))

        adj_cflags = [f"-I{tmp_inc}", f"-I{inc_dir}", f"-I{os.path.join(src_root, 'sys')}", f"-I{os.path.join(libc_dir, 'include')}"]
        # Help gcc -M find headers inside the contrib trees themselves
        # (gdtoa.h, private.h, tzfile.h, etc).
        for d in LIBC_CONTRIB_DIRS:
            adj_cflags.append(f"-I{os.path.join(src_root, d)}")
        for part in re.split(r'\s+', cflags):
            if part.startswith("-I"):
                p = part[2:]
                if not os.path.isabs(p): p = os.path.abspath(os.path.join(libc_dir, p))
                adj_cflags.append(f"-I{p}")
            elif part.startswith("-D"): adj_cflags.append(part)

        processed = 0
        scan_failures = []  # sources gcc -M could not preprocess (deps not discovered)
        for src in resolved_srcs:
            processed += 1
            if not src.endswith(('.c', '.S', '.s')): continue
            # Find sys/ headers we might have missed. warn=False: failures are
            # summarized below instead of warned per-file (gdtoa sources fail
            # because their generated arith.h/gd_qnan.h aren't on the path).
            rc, deps_raw = run_command(f"gcc -M {' '.join(adj_cflags)} {src}", warn=False)
            if rc != 0:
                scan_failures.append(src)
            if deps_raw:
                try:
                    # Clean up the gcc -M output which contains backslashes and newlines
                    clean_deps = deps_raw.replace('\\\n', ' ').replace('\\', ' ')
                    deps = re.split(r'\s+', clean_deps.split(':', 1)[1])
                    for d in deps:
                        if d:
                            abs_d = os.path.abspath(os.path.realpath(d))
                            # Add dependencies if they are in sys/ or contrib/
                            if abs_d.startswith(os.path.join(src_root, "sys")) or abs_d.startswith(os.path.join(src_root, "contrib")):
                                all_files.add(abs_d)
                except Exception as e:
                    pass
            if processed % 100 == 0: print(f"  Scanned {processed}/{len(resolved_srcs)} sources for sys/ dependencies...")
        shutil.rmtree(tmp_inc)

        if scan_failures:
            print(f"  [warn] gcc -M could not scan {len(scan_failures)}/{len(resolved_srcs)} "
                  f"source(s) on {arch}; their external (sys/contrib) deps were not "
                  f"auto-discovered. Unconditionally-copied trees (e.g. gdtoa) are still "
                  f"fully present; this only affects deps reached *through* them.")
            for s in scan_failures[:10]:
                print(f"          {os.path.relpath(s, src_root)}")
            if len(scan_failures) > 10:
                print(f"          ... and {len(scan_failures) - 10} more")

    # Final pass: drop excluded files no matter which step added them.
    for d, names in CONTRIB_EXCLUDE.items():
        prefix = os.path.join(src_root, d) + os.sep
        all_files = {
            f for f in all_files
            if not (f.startswith(prefix) and os.path.basename(f) in names)
        }

    print(f"\nTotal files to extract: {len(all_files)}")
    for f in all_files:
        rel = os.path.relpath(f, src_root)
        dest = os.path.join(output_dir, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(f, dest)
    print(f"Done. Extracted to {output_dir}")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 extract_libc_v4.py <freebsd_src_root> <output_dir>")
        sys.exit(1)
    extract_libc(sys.argv[1], sys.argv[2])
