#!/bin/sh
# Install KubeDeploy for the local operator account (does not install Kubernetes).
set -eu
if [ "$(id -u)" -ne 0 ]; then
    echo "请使用 root 运行：sudo ./install.sh" >&2
    exit 1
fi
SRC=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEST=/opt/kubedeploy-cli
rm -rf "$DEST"
mkdir -p "$DEST"
cp -a "$SRC/k8s-deploy" "$SRC/k8s-deploy.py" "$SRC/kubedeploy" "$SRC/docs" "$SRC/README.md" "$SRC/CHANGELOG.md" "$SRC/LICENSE" "$DEST/"
chmod 755 "$DEST/k8s-deploy" "$DEST/k8s-deploy.py"
ln -sfn "$DEST/k8s-deploy" /usr/local/bin/kubedeploy
echo "已安装：/usr/local/bin/kubedeploy"
echo "运行：kubedeploy --help  或  kubedeploy"
