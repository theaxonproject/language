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
out_dir="$repo_root/packaging/out/linux"
work_dir="$repo_root/packaging/out/work/linux-$target"
root_dir="$work_dir/root"

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

case "$target" in
    x86_64-*) deb_arch="amd64" ;;
    aarch64-*) deb_arch="arm64" ;;
    *)
        echo "Unsupported Linux packaging target: $target" >&2
        exit 1
        ;;
esac
rpm_arch="noarch"

rm -rf "$work_dir"
mkdir -p "$root_dir/usr/local/axon" "$root_dir/usr/local/bin" "$out_dir"
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

tar_stage="$work_dir/tar"
mkdir -p "$tar_stage"
cp -R "$root_dir/usr/local/axon" "$tar_stage/axon"
cat > "$tar_stage/install.sh" <<SCRIPT
#!/usr/bin/env sh
set -e
prefix="\${1:-/usr/local}"
mkdir -p "\$prefix"
cp -R axon "\$prefix/axon"
mkdir -p "\$prefix/bin"
for tool in axc axbuild axpkg axls axfmt axrepl axlint axup; do
    ln -sf "../axon/bin/\$tool" "\$prefix/bin/\$tool"
done
echo "AXON $package_version installed. Run \`axbuild new hello\` to create your first project."
SCRIPT
chmod 0755 "$tar_stage/install.sh"

tarball="$out_dir/axon-$package_version-$target.tar.gz"
tar --sort=name \
    --owner=0 \
    --group=0 \
    --numeric-owner \
    --mtime="@0" \
    -C "$tar_stage" \
    -czf "$tarball" \
    axon install.sh

deb_root="$work_dir/deb"
mkdir -p "$deb_root/DEBIAN"
cp -R "$root_dir"/. "$deb_root/"
installed_size="$(du -sk "$deb_root/usr/local/axon" | awk '{print $1}')"

cat > "$deb_root/DEBIAN/control" <<CONTROL
Package: axon
Version: $package_version
Section: devel
Priority: optional
Architecture: $deb_arch
Installed-Size: $installed_size
Maintainer: Marcel R. <m5rcel@european-commission-europa.eu>
Homepage: https://m5rcel.neocities.org/axon
Description: AXON programming language toolchain
 AXON is a statically typed native programming language toolchain.
CONTROL

cat > "$deb_root/DEBIAN/postinst" <<SCRIPT
#!/usr/bin/env sh
set -e
echo "AXON $package_version installed. Run \`axbuild new hello\` to create your first project."
SCRIPT
chmod 0755 "$deb_root/DEBIAN/postinst"

fakeroot dpkg-deb --build --root-owner-group "$deb_root" "$out_dir/axon-$package_version-$target.deb"

rpm_top="$work_dir/rpm"
rpm_buildroot="$rpm_top/BUILDROOT/axon-$package_version-1.$rpm_arch"
mkdir -p "$rpm_top"/{BUILD,RPMS,SOURCES,SPECS,SRPMS} "$rpm_buildroot"
cp -R "$root_dir"/. "$rpm_buildroot/"
tar -C "$rpm_buildroot" -czf "$rpm_top/SOURCES/axon-payload.tar.gz" .

cat > "$rpm_top/SPECS/axon.spec" <<SPEC
Name: axon
Version: $package_version
Release: 1%{?dist}
Summary: AXON programming language toolchain
License: MIT
URL: https://m5rcel.neocities.org/axon
BuildArch: $rpm_arch
Source0: axon-payload.tar.gz

%description
AXON is a statically typed native programming language toolchain.

%install
mkdir -p %{buildroot}
tar -xzf %{SOURCE0} -C %{buildroot}

%post
echo "AXON $package_version installed. Run \`axbuild new hello\` to create your first project."

%files
/usr/local/axon
/usr/local/bin/axc
/usr/local/bin/axbuild
/usr/local/bin/axpkg
/usr/local/bin/axls
/usr/local/bin/axfmt
/usr/local/bin/axrepl
/usr/local/bin/axlint
/usr/local/bin/axup
SPEC

rpmbuild \
    --define "_topdir $rpm_top" \
    -bb "$rpm_top/SPECS/axon.spec"

find "$rpm_top/RPMS" -name '*.rpm' -exec cp {} "$out_dir/axon-$package_version-$target.rpm" \;

echo "Built $tarball"
echo "Built $out_dir/axon-$package_version-$target.deb"
echo "Built $out_dir/axon-$package_version-$target.rpm"
