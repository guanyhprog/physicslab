# Oracle Cloud 后端部署指南

## 1. 创建 Oracle Cloud 账户

1. 访问 https://www.oracle.com/cloud/free/
2. 注册免费账户（需要信用卡验证，但不会收费）
3. 登录后进入 Console

## 2. 创建计算实例

### 选择实例配置
- **实例类型**: VM.Standard.A1.Flex (ARM) 或 VM.Standard.E2.1.Micro (AMD)
- **操作系统**: Ubuntu 22.04 或 Oracle Linux 8
- **形状**: VM.Standard.A1.Flex (推荐，4 OCPU + 24GB RAM)
- **网络**: 创建 VCN（虚拟云网络）

### 重要：添加 SSH 密钥
- 下载私钥文件（.key）
- 妥善保管，后续登录需要

## 3. 配置安全列表（防火墙）

在 VCN 的安全列表中添加入站规则：

| 协议 | 源 CIDR | 端口范围 | 说明 |
|------|---------|----------|------|
| TCP | 0.0.0.0/0 | 22 | SSH |
| TCP | 0.0.0.0/0 | 5000 | Flask 应用 |
| TCP | 0.0.0.0/0 | 80 | HTTP (可选) |

## 4. 连接到实例

```bash
ssh -i <你的私钥文件> ubuntu@<实例公网IP>
```

## 5. 安装依赖

```bash
# 更新系统
sudo apt update && sudo apt upgrade -y

# 安装 Python 和 pip
sudo apt install python3 python3-pip python3-venv -y

# 安装 Git
sudo apt install git -y
```

## 6. 部署应用

```bash
# 克隆仓库
git clone https://github.com/guanyhprog/physicslab.git
cd physicslab

# 创建虚拟环境
python3 -m venv venv
source venv/bin/activate

# 安装依赖
pip install -r requirements.txt
```

## 7. 运行应用

```bash
# 使用 Gunicorn 生产服务器
gunicorn app:app --bind 0.0.0.0:5000 --timeout 300
```

## 8. 设置后台运行（可选）

### 使用 systemd 服务

创建服务文件：
```bash
sudo nano /etc/systemd/system/physicslab.service
```

内容：
```ini
[Unit]
Description=Physics Lab Flask App
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/physicslab
Environment="PATH=/home/ubuntu/physicslab/venv/bin"
ExecStart=/home/ubuntu/physicslab/venv/bin/gunicorn app:app --bind 0.0.0.0:5000 --timeout 300
Restart=always

[Install]
WantedBy=multi-user.target
```

启动服务：
```bash
sudo systemctl daemon-reload
sudo systemctl start physicslab
sudo systemctl enable physicslab
```

## 9. 获取公网 IP

在 Oracle Cloud Console 中查看实例的公网 IP 地址。

## 10. 配置前端

访问 GitHub Pages 时，在 URL 后添加参数：
```
https://guanyhprog.github.io/physicslab/?api_url=http://<你的OracleCloud公网IP>:5000
```

## 注意事项

- Oracle Cloud 免费层：ARM 实例每月 750 小时（最多 4 个 OCPU，24GB RAM）
- 实例会在空闲时继续运行，不会自动休眠
- 公网 IP 是固定的（如果分配了预留公网 IP）
- 建议使用 HTTPS（可以用 Let's Encrypt 免费证书）
