#!/bin/bash
#git pull
git submodule init
git submodule update
git submodule foreach git pull origin master

cd sealdice-core
git config pull.rebase false
git fetch origin dev
git checkout dev
git reset origin/dev --hard
cd ..

git commit -am "chore: bump submodules"
git push
