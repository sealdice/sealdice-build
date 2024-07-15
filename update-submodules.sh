#!/bin/bash
git pull
git submodule init
git submodule update

git submodule foreach 'git fetch origin --prune'
cd go-cqhttp && git reset --hard origin/master && cd ..
cd sealdice-android && git reset --hard origin/master && cd ..
cd sealdice-builtins && git reset --hard origin/master && cd ..
cd sealdice-core && git reset --hard origin/main && cd ..
cd sealdice-ui && git reset --hard origin/master && cd ..

git commit -am "chore: bump submodules"
git push
