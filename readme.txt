sudo nano /etc/systemd/system/testbook.service
sudo systemctl stop testbook.service
sudo systemctl start testbook.service
systemctl status testbook.service -l
sudo journalctl -f -u testbook.service