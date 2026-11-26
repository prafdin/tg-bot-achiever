#!/usr/bin/env bash
set -euo pipefail

if [[ ! $VERSION =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ || ! $RUN_ID =~ ^[1-9][0-9]{0,19}$ ]]; then
    echo 'Укажи версию MAJOR.MINOR.PATCH и числовой ID сборки.' >&2
    exit 2
fi

repository=$GITHUB_REPOSITORY
image="ghcr.io/${repository,,}"
tag="v$VERSION"
revision=$(gh api "repos/$repository/actions/runs/$RUN_ID" | jq -er --arg repository "$repository" '
    select(.conclusion == "success" and .head_branch == "main"
           and .path == ".github/workflows/build.yml" and .head_repository.full_name == $repository)
    | .head_sha | select(test("^[0-9a-f]{40}$"))
')
package_version=$(git show "$revision:pyproject.toml" | python3 -c '
import sys, tomllib
print(tomllib.loads(sys.stdin.read())["project"]["version"])
')
if [[ $package_version != "$VERSION" ]]; then
    echo "Версия выбранной сборки — $package_version, запрошен релиз $VERSION." >&2
    exit 1
fi

tag_revision=$(git rev-parse --verify --quiet "$tag^{commit}" || true)
if [[ -n $tag_revision && $tag_revision != "$revision" ]]; then
    echo "Git-тег $tag уже указывает на другой коммит." >&2
    exit 1
fi

workspace=$(mktemp -d)
trap 'rm -rf -- "$workspace"' EXIT
printf '%s\n' "$GH_TOKEN" | docker login ghcr.io --username "$GITHUB_ACTOR" --password-stdin

image_digest() {
    docker buildx imagetools inspect "$1" | awk '$1 == "Digest:" && !found++ { print $2 }'
}

candidate="$image:build-$RUN_ID"
digest=$(image_digest "$candidate")
[[ $digest =~ ^sha256:[0-9a-f]{64}$ ]]
pinned="$image@$digest"
docker pull "$pinned"
docker image inspect "$pinned" --format '{{json .Config.Labels}}' |
    jq -e --arg source "https://github.com/$repository" --arg revision "$revision" --arg build "build-$RUN_ID" '
        .["org.opencontainers.image.source"] == $source
        and .["org.opencontainers.image.revision"] == $revision
        and .["org.opencontainers.image.version"] == $build
    ' >/dev/null
jq -n --arg version "$VERSION" --arg run_id "$RUN_ID" --arg revision "$revision" --arg image "$pinned" \
    '{version: $version, run_id: $run_id, revision: $revision, image: $image}' > "$workspace/release.json"

release_exists=false
if gh release view "$tag" --repo "$repository" >/dev/null 2>&1; then
    release_exists=true
    gh release download "$tag" --repo "$repository" --pattern release.json --output "$workspace/existing.json"
    if ! cmp -s "$workspace/release.json" "$workspace/existing.json"; then
        echo "Релиз $tag уже закреплён за другой сборкой." >&2
        exit 1
    fi
fi

if existing=$(image_digest "$image:$VERSION" 2>"$workspace/inspect-error"); then
    if [[ $existing != "$digest" ]]; then
        echo "Тег образа $VERSION уже закреплён за другим дайджестом." >&2
        exit 1
    fi
elif grep -qi 'not found' "$workspace/inspect-error"; then
    docker buildx imagetools create --prefer-index=false --tag "$image:$VERSION" "$pinned"
else
    cat "$workspace/inspect-error" >&2
    exit 1
fi
[[ $(image_digest "$image:$VERSION") == "$digest" ]]

if [[ -z $tag_revision ]]; then
    gh api --method POST "repos/$repository/git/refs" -f ref="refs/tags/$tag" -f sha="$revision" --jq .ref
fi
if [[ $release_exists == false ]]; then
    gh release create "$tag" "$workspace/release.json" --repo "$repository" --verify-tag --generate-notes
fi
printf 'Release %s: build %s, commit %s, image %s\n' "$tag" "$RUN_ID" "$revision" "$pinned"
