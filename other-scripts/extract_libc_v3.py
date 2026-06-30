import os
import subprocess
import shutil
import re
import sys

def run_command(cmd, cwd=None):
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=cwd)
    if result.returncode != 0:
        return result.stdout.strip()
    return result.stdout.strip()

def extract_libc(src_root, output_dir, arches=["amd64", "aarch64"]):
    src_root = os.path.abspath(src_root)
    output_dir = os.path.abspath(output_dir)
    libc_dir = os.path.join(src_root, "lib/libc")
    inc_dir = os.path.join(src_root, "include")
    mk_dir = os.path.join(src_root, "share/mk")

    print(f"Analyzing FreeBSD source at {src_root}...")
    all_files = set()

    # 1. Unconditionally include all of lib/libc, include, and share/mk for original structure
    print("Collecting all files from lib/libc, include, and share/mk...")
    for root_dir in [libc_dir, inc_dir, mk_dir]:
        if not os.path.exists(root_dir):
            print(f"Warning: {root_dir} does not exist.")
            continue
        for root, dirs, files in os.walk(root_dir):
            for f in files:
                all_files.add(os.path.join(root, f))

    # 2. Use arch-specific analysis ONLY to find dependencies in other dirs (like sys/)
    for arch in arches:
        print(f"\nProcessing dependencies for architecture: {arch}")
        # Get SRCS and .PATH to find what is actually used to build libc on this arch
        srcs_raw = run_command(f"bmake -m {mk_dir} MACHINE_ARCH={arch} SRCTOP={src_root} -V SRCS", cwd=libc_dir)
        path_raw = run_command(f"bmake -m {mk_dir} MACHINE_ARCH={arch} SRCTOP={src_root} -V .PATH", cwd=libc_dir)
        cflags = run_command(f"bmake -m {mk_dir} MACHINE_ARCH={arch} SRCTOP={src_root} -V CFLAGS", cwd=libc_dir)
        
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
        for part in re.split(r'\s+', cflags):
            if part.startswith("-I"):
                p = part[2:]
                if not os.path.isabs(p): p = os.path.abspath(os.path.join(libc_dir, p))
                adj_cflags.append(f"-I{p}")
            elif part.startswith("-D"): adj_cflags.append(part)

        processed = 0
        for src in resolved_srcs:
            processed += 1
            if not src.endswith(('.c', '.S', '.s')): continue
            # This finds headers in sys/ that we might have missed
            deps_raw = run_command(f"gcc -M {' '.join(adj_cflags)} {src}")
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

    print(f"\nTotal files to extract: {len(all_files)}")
    for f in all_files:
        rel = os.path.relpath(f, src_root)
        dest = os.path.join(output_dir, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(f, dest)
    print(f"Done. Extracted to {output_dir}")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 extract_libc_full.py <freebsd_src_root> <output_dir>")
        sys.exit(1)
    extract_libc(sys.argv[1], sys.argv[2])
