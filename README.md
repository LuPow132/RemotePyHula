
# RemotePyHula

short explanation on how to install all library that require to run this program



## Installation

Install for window user

to install cloudflare
```bash
  winget install --id Cloudflare.cloudflared
```

for python library
```bash
  pip install flask flask-sock
```


## Deploy Program

To deploy this project open the code and run the script you want to uses with. for example

```bash
  python controller.py
```
(you can either run on your prefer IDE or on terminal)

after drone connection and everything then you can start running cloudflare service to generate link which can be share to public for anyone to use later 

```bash
  cloudflared tunnel --url http://localhost:8080
```

the link you get will look close to this

```bash
2026-05-08T09:28:10Z INF +--------------------------------------------------------------------------------------------+
2026-05-08T09:28:10Z INF |  Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):  |
2026-05-08T09:28:10Z INF |  https://andale-lawrence-advertise-pursue.trycloudflare.com                                |
2026-05-08T09:28:10Z INF +--------------------------------------------------------------------------------------------+
```

then your link that can be shared will be 
```bash
https://andale-lawrence-advertise-pursue.trycloudflare.com   
```
