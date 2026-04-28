import os
import subprocess
import shutil
import re
import sys

def run_command(cmd, cwd=None):
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=cwd)
    if result.returncode != 0:
        # Some commands might fail but still give output (e.g. gcc -M with missing headers)
        return result.stdout.strip()
    return result.stdout.strip()

def extract_libc(src_root, output_dir, arches=["amd64", "aarch64"]):
    src_root = os.path.abspath(src_root)
    output_dir = os.path.abspath(output_dir)
    libc_dir = os.path.join(src_root, "lib/libc")
    mk_dir = os.path.join(src_root, "share/mk")

    print(f"Analyzing FreeBSD source at {src_root}...")
    all_files = set()

    # Collect some base files
    for root, dirs, files in os.walk(mk_dir):
        for f in files:
            all_files.add(os.path.join(root, f))

    for arch in arches:
        print(f"\nProcessing architecture: {arch}")
        # Get SRCS
        srcs_raw = run_command(f"bmake -m {mk_dir} MACHINE_ARCH={arch} SRCTOP={src_root} -V SRCS", cwd=libc_dir)
        if not srcs_raw: continue
        srcs = srcs_raw.split()

        # Get .PATH
        path_raw = run_command(f"bmake -m {mk_dir} MACHINE_ARCH={arch} SRCTOP={src_root} -V .PATH", cwd=libc_dir)
        if not path_raw: continue
        paths = path_raw.split()

        # Get CFLAGS
        cflags = run_command(f"bmake -m {mk_dir} MACHINE_ARCH={arch} SRCTOP={src_root} -V CFLAGS", cwd=libc_dir)
        if not cflags: continue

        # Resolve SRCS
        resolved_srcs = []
        for s in srcs:
            for p in paths:
                if not os.path.isabs(p): p = os.path.abspath(os.path.join(libc_dir, p))
                full_path = os.path.join(p, s)
                if os.path.exists(full_path):
                    resolved_srcs.append(os.path.abspath(full_path))
                    break

        all_files.update(resolved_srcs)

        # Symlinks for this arch
        tmp_inc = os.path.join(os.getcwd(), f"tmp_inc_{arch}")
        if os.path.exists(tmp_inc): shutil.rmtree(tmp_inc)
        os.makedirs(tmp_inc)

        # Map arch to FreeBSD machine dir
        mach_dir = arch
        if arch == "aarch64": mach_dir = "arm64" # FreeBSD uses arm64 dir for aarch64
        elif arch == "amd64": mach_dir = "amd64"

        os.symlink(os.path.join(src_root, f"sys/{mach_dir}/include"), os.path.join(tmp_inc, "machine"))
        os.symlink(os.path.join(src_root, "sys/x86/include"), os.path.join(tmp_inc, "x86"))

        adj_cflags = [f"-I{tmp_inc}", f"-I{os.path.join(src_root, 'include')}", 
                      f"-I{os.path.join(src_root, 'sys')}", f"-I{os.path.join(src_root, 'lib/libc/include')}"]
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
            deps_raw = run_command(f"gcc -M {' '.join(adj_cflags)} {src}")
            if deps_raw:
                try:
                    deps = re.split(r'\s+', deps_raw.split(':', 1)[1].replace('\\\n', ' ').replace('\\', ' '))
                    for d in deps:
                        if d:
                            abs_d = os.path.abspath(os.path.realpath(d))
                            if abs_d.startswith(src_root): all_files.add(abs_d)
                except: pass
            if processed % 100 == 0: print(f"  Processed {processed}/{len(resolved_srcs)} sources...")
        shutil.rmtree(tmp_inc)

    # Collect Makefiles, etc. in directories we used
    print("\nCollecting auxiliary files (Makefiles, maps, etc.)...")
    used_dirs = set(os.path.dirname(f) for f in all_files)
    for d in list(used_dirs):
        if not d.startswith(src_root): continue
        for f in os.listdir(d):
            if f.startswith("Makefile") or f.endswith((".inc", ".map", ".def", ".mk")):
                all_files.add(os.path.join(d, f))

    print(f"Total files to extract: {len(all_files)}")
    for f in all_files:
        rel = os.path.relpath(f, src_root)
        dest = os.path.join(output_dir, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(f, dest)
    print(f"Done. Extracted to {output_dir}")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 extract_libc.py <freebsd_src_root> <output_dir>")
        sys.exit(1)
    extract_libc(sys.argv[1], sys.argv[2])
