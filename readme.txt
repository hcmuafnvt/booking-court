sudo nano /etc/systemd/system/testbook.service
sudo systemctl stop testbook.service
sudo systemctl start testbook.service
View log : systemctl status testbook.service -l
View log real-time : sudo journalctl -f -u testbook.service