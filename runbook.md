# Runbook — booking-court

## Kết nối EC2

```bash
sudo ssh -i "/Users/toan/TOAN/MyProjects/toan_ssh_keys/MyBcHomes.pem" \
  ec2-user@ec2-54-147-50-51.compute-1.amazonaws.com
```

> **Thứ tự đúng khi deploy máy mới**: kiểm tra máy → setup (clone + venv + Chromium)
> → copy config → chạy thử → cài service. Phải clone repo TRƯỚC rồi mới copy config,
> vì `scp` cần thư mục đích đã tồn tại.

## Kiểm tra cấu hình máy

```bash
nproc                                    # số CPU
free -h                                  # RAM + swap (Swap total = 0 là chưa có swap)
df -h /                                  # đĩa
head -2 /etc/os-release                  # Amazon Linux hay Ubuntu
python3 --version                        # cần >= 3.9
ps -eo pid,rss,comm --sort=-rss | head   # tiến trình ngốn RAM nhất (rss tính bằng KB)
sudo dmesg | grep -i "killed process"    # máy đã từng hết RAM chưa
```

Loại instance (`t2.micro` / `t3.micro` = free tier, 1 GB RAM):

```bash
TOKEN=$(curl -sX PUT "http://169.254.169.254/latest/api/token" \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
  http://169.254.169.254/latest/meta-data/instance-type
```

## Setup lần đầu trên EC2 mới

```bash
git clone https://github.com/hcmuafnvt/booking-court.git ~/booking-court
cd ~/booking-court

python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

Cài Chromium — **Ubuntu / Debian**:

```bash
./venv/bin/python -m playwright install --with-deps chromium
```

Cài Chromium — **Amazon Linux** (`--with-deps` không hỗ trợ, phải cài thư viện tay):

```bash
sudo dnf install -y nss atk at-spi2-atk cups-libs libdrm libXcomposite \
  libXdamage libXrandr mesa-libgbm alsa-lib pango libxkbcommon
./venv/bin/python -m playwright install chromium
```

Xoá session cũ — cookie gắn với IP máy cũ nên vô dụng trên EC2 mới:

```bash
rm -f ~/booking-court/pickleball/toan_session.json
```

Thêm swap 2 GB nếu free tier 1 GB RAM và chạy chung với bot đặt sân:

```bash
sudo dd if=/dev/zero of=/swapfile bs=1M count=2048
sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## Copy file config

> Chạy trên **máy Mac**, không phải trên EC2.
> `pickleball/config.json` nằm trong `.gitignore` nên `git clone` không có nó — bước này bắt buộc.
> Phải clone repo trên EC2 xong rồi mới chạy được, nếu không `scp` báo
> `dest open ... No such file or directory`.

```bash
cd /Users/toan/TOAN/MyProjects/booking-court

scp -i "/Users/toan/TOAN/MyProjects/toan_ssh_keys/MyBcHomes.pem" \
  pickleball/config.json \
  ec2-user@ec2-54-147-50-51.compute-1.amazonaws.com:~/booking-court/pickleball/
```

Kiểm tra trên EC2 — phải là `true` vì EC2 không có màn hình:

```bash
grep headless_mode ~/booking-court/pickleball/config.json
```

## Chạy thử trước khi bật service

```bash
cd ~/booking-court
./venv/bin/python pickleball/watch_open_time.py probe
```

Đúng thì ra `=> mở trước 14 ngày` hoặc `15 ngày`.

| Lỗi | Nguyên nhân |
| --- | --- |
| `KeyError: 'maspow'` | thiếu `config.json` → quay lại mục Copy file config |
| lỗi khi mở browser | thiếu thư viện hệ thống → quay lại mục cài Chromium |
| `=> mở trước 7 ngày` | login hỏng hoặc Cloudflare chặn IP EC2 |
| `=> đọc không được` | mạng hoặc Cloudflare |

## Cài service theo dõi giờ mở sân

Sửa đường dẫn nếu repo không nằm ở `/home/ec2-user/booking-court`:

```bash
pwd && whoami
nano ~/booking-court/deploy/court-watch.service   # sửa User, WorkingDirectory, ExecStart
```

Cài và bật:

```bash
sudo cp ~/booking-court/deploy/court-watch.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now court-watch
```

30 giây đầu phải thấy `Lần đọc đầu tiên: max=2026-09-1x`, sau đó im lặng là đúng.

## Xem log service

```bash
sudo journalctl -f -u court-watch                      # theo thời gian thực
sudo journalctl -u court-watch -n 50 --no-pager        # 50 dòng cuối
sudo journalctl -u court-watch --no-pager | grep "★"   # chỉ những lần mở sân
systemctl status court-watch -l                        # đang chạy hay đã chết
```

## Xem report giờ mở sân

```bash
cd ~/booking-court
./venv/bin/python pickleball/watch_open_time.py report
```

Đọc 2 dòng cuối `Giờ mở` và `Mở trước` là ra kết quả.

Nếu chưa có dòng nào, kiểm tra có lỗi đọc liên tục không:

```bash
grep -c '"type":"error"' ~/booking-court/pickleball/open_watch.jsonl
cat ~/booking-court/pickleball/open_watch.jsonl
```

## Start / stop / restart

```bash
sudo systemctl start court-watch
sudo systemctl stop court-watch
sudo systemctl restart court-watch    # sau khi git pull hoặc sửa code
sudo systemctl daemon-reload          # sau khi sửa file .service
```

## Cập nhật code

```bash
cd ~/booking-court
git pull
./venv/bin/pip install -r requirements.txt
sudo systemctl restart court-watch
```

## Gỡ service sau khi đo xong (khoảng 1 tuần)

Lưu kết quả trước khi xoá:

```bash
cd ~/booking-court && ./venv/bin/python pickleball/watch_open_time.py report
```

Rồi gỡ:

```bash
sudo systemctl disable --now court-watch
sudo rm /etc/systemd/system/court-watch.service
sudo systemctl daemon-reload
```

## Service bot đặt sân (đang có sẵn)

```bash
sudo nano /etc/systemd/system/testbook.service
sudo systemctl stop testbook.service
sudo systemctl start testbook.service
systemctl status testbook.service -l
sudo journalctl -f -u testbook.service
sudo journalctl -u testbook.service -n 30 --no-pager | grep -i "OnFailure\|warning\|Error"
```
