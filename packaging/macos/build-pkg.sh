#!/usr/bin/env bash
set -euo pipefail

raw_version="${1:?version is required}"
target="${2:?target triple is required}"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
version="${raw_version#v}"
if [[ ! "$version" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$ ]]; then
    echo "Invalid AXON version '$raw_version'. Expected semantic version such as 1.0.0." >&2
    exit 1
fi
package_version="${BASH_REMATCH[1]}.${BASH_REMATCH[2]}.${BASH_REMATCH[3]}"
dist_root="$repo_root/dist/$target"
out_dir="$repo_root/packaging/out/macos"
work_dir="$repo_root/packaging/out/work/macos-$target"
root_dir="$work_dir/root"
scripts_dir="$work_dir/scripts"

if [[ ! -d "$dist_root" ]]; then
    echo "Staged distribution not found: $dist_root" >&2
    exit 1
fi

required_tools=(axc axbuild axpkg axls axfmt axrepl axlint axup)
for tool in "${required_tools[@]}"; do
    if [[ ! -f "$dist_root/bin/$tool" ]]; then
        echo "Required AXON tool is missing: $dist_root/bin/$tool" >&2
        exit 1
    fi
done
if [[ ! -f "$dist_root/tools/axon_tool.py" ]]; then
    echo "Required AXON implementation is missing: $dist_root/tools/axon_tool.py" >&2
    exit 1
fi

rm -rf "$work_dir"
mkdir -p "$root_dir/usr/local/axon" "$root_dir/usr/local/bin" "$scripts_dir" "$out_dir"
cp -R "$dist_root"/. "$root_dir/usr/local/axon/"

if [[ ! -f "$root_dir/usr/local/axon/LICENSE" ]]; then
    cp "$repo_root/LICENSE" "$root_dir/usr/local/axon/LICENSE"
fi
if [[ ! -d "$root_dir/usr/local/axon/docs" ]]; then
    cp -R "$repo_root/docs" "$root_dir/usr/local/axon/docs"
fi

for tool in "${required_tools[@]}"; do
    chmod 0755 "$root_dir/usr/local/axon/bin/$tool"
    ln -s "../axon/bin/$tool" "$root_dir/usr/local/bin/$tool"
done

cat > "$scripts_dir/postinstall" <<SCRIPT
#!/usr/bin/env bash
set -euo pipefail
echo "AXON $package_version installed. Run \`axbuild new hello\` to create your first project."
SCRIPT
chmod 0755 "$scripts_dir/postinstall"

component_pkg="$work_dir/axon-component.pkg"
final_pkg="$out_dir/axon-$package_version-$target.pkg"

pkgbuild \
    --root "$root_dir" \
    --identifier "dev.axon-lang.axon.$target" \
    --version "$package_version" \
    --install-location "/" \
    --scripts "$scripts_dir" \
    "$component_pkg"

productbuild \
    --package "$component_pkg" \
    "$final_pkg"

if [[ -n "${APPLE_INSTALLER_IDENTITY:-}" ]]; then
    signed_pkg="$out_dir/axon-$package_version-$target-signed.pkg"
    productsign --sign "$APPLE_INSTALLER_IDENTITY" "$final_pkg" "$signed_pkg"
    mv "$signed_pkg" "$final_pkg"
fi

echo "Built $final_pkg"
