#!/usr/bin/env bash
# 构建 Ace security-runtime（Linux/macOS 本机产物）并落 bin/ + 更新 source-hash 清单。
# 触发时机：改了 security-runtime/ 下的 Rust 源码或 Cargo.toml，重跑本脚本再 commit。
# 产出：security-runtime/bin/ace-security-runtime（当前 Unix 平台原生二进制）
#       security-runtime/bin/runtime-manifest.json（source_hash）
# 注意：产物与构建主机平台绑定。Windows 请使用 .ps1 版本。
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
crate="$repo_root/security-runtime"
bin_dir="$crate/bin"
target="ace-security-runtime"

echo "[1/3] cargo build --release (本机默认 target)..."
( cd "$crate" && cargo build --release )
built="$crate/target/release/$target"
[ -f "$built" ] || { echo "build 产物未找到：$built" >&2; exit 1; }
mkdir -p "$bin_dir"
cp -f "$built" "$bin_dir/$target"
echo "[2/3] copied -> $bin_dir/$target"

echo "[3/3] regenerating runtime-manifest.json (source + binary hash)..."
python3 - "$crate" "$bin_dir" "$bin_dir/$target" <<'PY'
import hashlib, json, pathlib, platform, sys
crate = pathlib.Path(sys.argv[1]); bin_dir = pathlib.Path(sys.argv[2]); exe = pathlib.Path(sys.argv[3])
files = sorted(p for p in [*crate.glob("src/**/*"), *crate.glob("tests/**/*.rs"), crate / "Cargo.toml", crate / "Cargo.lock"] if p.is_file())
h = hashlib.sha256()
for p in files:
    h.update(p.relative_to(crate).as_posix().encode()); h.update(b"\0")
    h.update(p.read_bytes()); h.update(b"\0")
manifest = {
    "schema": 2,
    "source_hash": h.hexdigest(),
    "binary_sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
    "binary_name": exe.name,
    "source_files": len(files),
    "built_for": f"{platform.system().lower()}-{platform.machine().lower()}",
    "note": "由 scripts/build-security-runtime.{ps1,sh} 重新生成；勿手改。source_hash 用于检测源码漂移，binary_sha256 用于 Python/Desktop 启动时校验二进制完整性。",
}
(bin_dir / "runtime-manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"    source_hash: {manifest['source_hash'][:16]}... binary_sha256: {manifest['binary_sha256'][:16]}... over {len(files)} files")
PY
echo "done. 请 git add security-runtime/bin/ 后提交。"
