#!/bin/bash

# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


# Checks that the certgen tag changes when GOLANG_VERSION does, and when certgen's directory does.
set -o errexit
set -o nounset
set -o pipefail

script="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/authagonal-certgen-tag.sh"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
cd "$work"
git init -q .
mkdir -p images/kube-webhook-certgen/rootfs
echo v1.0.0 > images/kube-webhook-certgen/TAG
echo package main > images/kube-webhook-certgen/rootfs/main.go
echo 1.26.2 > GOLANG_VERSION
git add -A
git -c user.name=t -c user.email=t@t commit -q -m one
before="$("$script")"

echo 1.26.3 > GOLANG_VERSION
git add -A
git -c user.name=t -c user.email=t@t commit -q -m golang
after_go="$("$script")"
[ "$before" != "$after_go" ] || { echo "FAIL: tag unchanged by a GOLANG_VERSION bump: $before"; exit 1; }

echo package other > images/kube-webhook-certgen/rootfs/main.go
git add -A
git -c user.name=t -c user.email=t@t commit -q -m source
after_src="$("$script")"
[ "$after_go" != "$after_src" ] || { echo "FAIL: tag unchanged by a certgen source change: $after_go"; exit 1; }
echo "ok: $before -> $after_go -> $after_src"
