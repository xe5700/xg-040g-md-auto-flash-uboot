# 贝尔XG-040G-MD一键刷入 使用教程
请使用Linux环境运行 WIN安装netcat也许可以运行 nmap安装后自带netcat包
WSL或者MSYS2环境也可以
非原生linux环境需要解除85行的注释并且注释掉83行
需要安装telnetlib3和nc coreutils软件包中已经包含了 一般发行版都用coreutils
pip install telnetlib3
如果没有，可以单独安装netcat
## archlinux
pacman -Sy netcat
## ubuntu/debian
apt install netcat
## centos/fedora
yum install netcat

请先重置路由器 按住reset 20秒以上
然后浏览器使用超级管理员帐号登录
使用web管理员 CMCCAdmin aDm8H%MdA 登录系统，开启文件共享ftp
然后根据你的光猫来修改脚本里的配置
然后运行flash.py

uboot文件请从nwrt网站的网盘获取
https://nwrt.kuroneko.host/