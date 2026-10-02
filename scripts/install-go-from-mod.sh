#!/bin/sh

set -eu

go_mod=${1:?usage: install-go-from-mod.sh PATH_TO_GO_MOD}

go_spec=$(awk '$1 == "toolchain" { print $2; exit }' "$go_mod")
if [ "$go_spec" = default ]; then
    go_spec=
fi
if [ -z "$go_spec" ]; then
    go_version=$(awk '$1 == "go" { print $2; exit }' "$go_mod")
    if [ -z "$go_version" ]; then
        echo "No toolchain or go directive found in $go_mod" >&2
        exit 1
    fi
    go_spec="go${go_version}"
fi

if ! printf '%s\n' "$go_spec" | grep -Eq '^go[0-9]+\.[0-9]+([.][0-9]+)?([a-z]+[0-9]+)?$'; then
    echo "Unsupported Go toolchain version in $go_mod: $go_spec" >&2
    exit 1
fi

case "$(uname -m)" in
    x86_64)
        go_arch=amd64
        ;;
    aarch64|arm64)
        go_arch=arm64
        ;;
    *)
        echo "Unsupported Linux architecture: $(uname -m)" >&2
        exit 1
        ;;
esac

go_metadata=/tmp/go-downloads.json
curl --fail --location --silent --show-error \
    'https://go.dev/dl/?mode=json&include=all' \
    --output "$go_metadata"

go_release=$(
    awk -F '"' -v version="$go_spec" \
        '$2 == "version" && $4 == version { print $4; exit }' \
        "$go_metadata"
)
if [ -z "$go_release" ] &&
    printf '%s\n' "$go_spec" | grep -Eq '^go[0-9]+\.[0-9]+$'; then
    go_release=$(
        awk -F '"' -v prefix="${go_spec}." \
            '$2 == "version" && index($4, prefix) == 1 { print $4 }' \
            "$go_metadata" |
            grep -E '^go[0-9]+\.[0-9]+\.[0-9]+$' |
            sort -Vr |
            head -n 1
    )
fi

if [ -z "$go_release" ]; then
    echo "Could not resolve $go_spec through the official Go download metadata" >&2
    exit 1
fi

go_filename="${go_release}.linux-${go_arch}.tar.gz"
go_sha256=$(
    awk -F '"' -v filename="$go_filename" '
        $2 == "filename" && $4 == filename { found = 1; next }
        found && $2 == "sha256" { print $4; exit }
        found && $2 == "filename" { exit }
    ' "$go_metadata"
)

if ! printf '%s\n' "$go_sha256" | grep -Eq '^[0-9a-f]{64}$'; then
    echo "No valid checksum found for $go_filename" >&2
    exit 1
fi

go_archive="/tmp/${go_filename}"
curl --fail --location --silent --show-error \
    "https://go.dev/dl/${go_filename}" \
    --output "$go_archive"
echo "${go_sha256}  ${go_archive}" | sha256sum -c -

rm -rf /usr/local/go
tar -C /usr/local -xzf "$go_archive"
/usr/local/go/bin/go version
