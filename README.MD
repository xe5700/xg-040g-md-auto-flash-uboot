# 贝尔XG-040G-MD 一键刷入使用教程

## 系统要求
- **请使用 Linux 环境运行**，不支持 Windows
- WSL 环境也可以运行

## 前置依赖安装

### Python 包
```bash
pip install telnetlib3
```

### 系统软件包
需要安装 `nc` (netcat)，一般包含在 coreutils 中：

#### Arch Linux
```bash
pacman -Sy netcat
```

#### Ubuntu/Debian
```bash
apt install netcat
```

#### CentOS/Fedora
```bash
yum install netcat
# 或者对于较新版本：
# dnf install netcat
```

## 准备工作

1. **重置路由器**
   - 按住 reset 键 20 秒以上，将设备恢复至出厂设置

2. **登录系统**
   - 使用浏览器登录光猫管理界面
   - **超级管理员账号**：`CMCCAdmin`
   - **密码**：`aDm8H%MdA`

3. **开启文件共享**
   - 登录后，在系统中开启 FTP 文件共享功能

## 配置和刷入

1. **修改脚本配置**
   - 根据你的光猫型号，修改 `flash.py` 脚本中的相关配置

2. **获取 uboot 文件**
   - 请从 NWRT 网站的网盘获取对应的 uboot 文件
   - 网站地址：https://nwrt.kuroneko.host/

3. **运行刷入脚本**
   ```bash
   python flash.py
   ```

## 注意事项
- 请确保在操作前已备份重要数据
- 刷机过程请勿断电，避免设备变砖
- 如遇问题，请参考 NWRT 网站的相关文档
