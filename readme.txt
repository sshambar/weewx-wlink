wlink - weewx driver that gets data from a weatherlink web site
Copyright 2014 Matthew Wall

Installation instructions:

1) expand the tarball:

cd /var/tmp
tar xvfz ~/Downloads/weewx-wlink.tgz

2) copy files to the weewx user directory:

cp /var/tmp/weewx-wlink/bin/user/wlink.py /home/weewx/bin/user

3) modify weewx.conf

[Station]
    station_type = WeatherLink

[WeatherLink]
    driver = user.wlink
    username = USERNAME
    password = PASSWORD

4) start weewx

sudo /etc/init.d/weewx start
