# Reaching Hondana from your phone and other devices

Hondana is a web server on one machine. Every other device (your phone, a tablet, Hondana for
iPhone) reaches it by an address. Pick the simplest option below that works for you. You can move
to a later one at any time.

> **Never forward Hondana's port on your router or expose it to the internet.** Even with an
> admin password, anyone who can reach it can browse and download your books. For access away
> from home, use a private VPN (option 5).

## 1. The IP address and the QR code (no setup)

Open Hondana on the machine it runs on, go to **Profile**, and scan the **Connect Hondana for
iPhone** QR code with your phone's camera. It opens the app already pointed at the server. The
same card shows the address to type, e.g. `http://192.168.1.20:8000`.

Hondana for iPhone also finds the server by itself over Bonjour when both are on the same Wi-Fi.

An IP address can change when the machine restarts. To keep it fixed, give the machine a **DHCP
reservation** in your router's settings (often under LAN or DHCP: pick the machine and "reserve"
or "always use this address").

Check, if the phone can't connect:

- the server listens on the network: `host = "0.0.0.0"` (the default);
- the machine's firewall allows the port (8000 by default);
- both devices are on the same network (guest Wi-Fi is often isolated);
- iOS **Settings › Privacy & Security › Local Network › Hondana** is on.

## 2. A `.local` name (mDNS, usually already working)

Macs, and most Linux machines with Avahi installed, answer to `<hostname>.local` on the LAN,
e.g. `http://bookshelf.local:8000`. iPhones and Macs resolve these names out of the box; Windows
10 and later mostly do too.

- **Linux**: `sudo apt install avahi-daemon` (Debian/Ubuntu) and the machine answers to its
  hostname. To answer to an extra name such as `books.local`, add a line to
  `/etc/avahi/hosts` (`192.168.1.20 books.local`) and restart avahi, or run
  `avahi-publish -a -R books.local 192.168.1.20` as a service.
- **Docker**: mDNS only works with host networking on Linux (`network_mode: host`). See
  [docker.md](docker.md#your-phone-and-the-network).

Use `.local` names only through mDNS. Don't put them in a DNS server (see option 3).

## 3. Your own DNS name

If your network has a DNS server you control (your router's, Pi-hole, AdGuard Home), add a name
for the machine:

| DNS server | Where |
|---|---|
| Pi-hole | **Local DNS › DNS Records**: `books.home.arpa` → `192.168.1.20` |
| AdGuard Home | **Filters › DNS rewrites** |
| Router | Often "Local DNS", "Static DNS" or "Hostnames", next to the DHCP reservation |

Choose a name under **`home.arpa`** (reserved for home networks by RFC 8375) or `.internal`.
Avoid `.local`, which belongs to mDNS and makes some devices skip your DNS server, and avoid a
real domain you don't own.

The name only works on devices that use that DNS server. Things that bypass it:

- **iCloud Private Relay** and "Limit IP Address Tracking". Turn them off for your home Wi-Fi in
  the iPhone's Wi-Fi settings, or use the IP address.
- **Secure DNS** (DNS over HTTPS) in browsers, and phones with a custom DNS profile.

Then open `http://books.home.arpa:8000` in a browser. To drop the `:8000`, add a reverse proxy
(option 4).

**Hondana for iPhone and DNS names.** iOS lets apps use plain `http://` only for local
addresses: an IP address, a `.local` name, or a single-word name. A name like
`books.home.arpa` needs `https://` in the app (option 5 is the easy way to get that). Keep using
the IP address or a `.local` name in the app, and the DNS name in browsers.

## 4. A reverse proxy (a name without the port, optionally HTTPS)

A reverse proxy such as Caddy or nginx serves Hondana on port 80/443 under a name. Bind Hondana to
this machine only, and tell discovery which port and name the proxy uses:

```toml
host = "127.0.0.1"

[discovery]
advertise_port = 80
advertise_host = "books.local"   # only if the proxy answers to this one name
```

Use a name the iPhone app can reach over plain `http://` (an mDNS alias from option 2 such as
`books.local`), or serve HTTPS. The examples use `books.local`.

**Caddy** (`/etc/caddy/Caddyfile`):

```
http://books.local {
    reverse_proxy 127.0.0.1:8000 {
        flush_interval -1
    }
    request_body {
        max_size 2GB
    }
}
```

**nginx**:

```nginx
server {
    listen 80;
    server_name books.local;
    client_max_body_size 2g;          # EPUB and dictionary uploads
    proxy_request_buffering off;      # stream big uploads straight through
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 1h;        # long downloads and pipeline runs
    }
}
```

Keep `Range` requests working (the defaults do). The phone resumes audiobook and dictionary
downloads with them.

**Audiobook uploads** from the web app are sent in 16 MB chunks, so they are not stopped by the
`2g` limit above, but by anything lower than 16 MB. nginx's default, `client_max_body_size 1m`,
answers 413 to every chunk; the page notices, retries with smaller chunks (down to 256 KB) and gets
the file through, only more slowly. Setting the limit to at least `16m` avoids that. Because a chunk is
a short request, `proxy_read_timeout` and buffering matter far less than they do for one huge upload.

**HTTPS on a home network** needs a certificate your phone trusts. A `home.arpa` name can't get a
public certificate, and installing your own certificate authority on every phone is fiddly. For
HTTPS, the Tailscale route below is easier. Hondana for iPhone allows plain `http://` to IP
addresses and `.local` names, so HTTPS isn't required at home if you use those.

## 5. Away from home: Tailscale

[Tailscale](https://tailscale.com) (free for personal use) puts your devices on a private network
that works from anywhere, without opening any port.

1. Install Tailscale on the Hondana machine and on your phone, and sign in to both.
2. Turn on **MagicDNS** and **HTTPS certificates** in the Tailscale admin console.
3. On the Hondana machine: `tailscale serve --bg 8000`. Hondana is now at
   `https://<machine>.<tailnet>.ts.net`, with a real certificate, from your devices only.
4. Tell Hondana that address, so the QR code and the app use it:

   ```toml
   public_url = "https://bookshelf.example-tailnet.ts.net"
   ```

   (or `HONDANA_PUBLIC_URL` in the environment).

Hondana for iPhone needs `https://` for names that aren't on the local network, which
`tailscale serve` provides. Other VPNs (WireGuard, a router's VPN server) work the same way: put
the phone on your home network and use any of the options above.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| The app doesn't list the server | Different network, Bonjour blocked (guest Wi-Fi, Docker bridge networking), or **Local Network** permission off. Use the QR code. |
| The QR code shows `localhost`, or a `172.x` address | You opened the page as `localhost`, or Hondana runs in Docker without host networking. Set `public_url`, or open the page by the machine's address. |
| The name works on the laptop but not the phone | The phone isn't using your DNS server (Private Relay, custom DNS). See option 3. |
| The name works in Safari but not in Hondana for iPhone | iOS blocks plain `http://` to names that aren't local (`.home.arpa`, your own domain). Use the IP or `.local` name in the app, or HTTPS. |
| `Connection refused` from other devices | `host` is `127.0.0.1` without a proxy, or a firewall blocks the port. |
| Uploads fail with 413 behind a proxy | Raise the proxy's body size limit (`client_max_body_size`, `request_body max_size`). Audiobook chunks are 16 MB: a lower limit works, slowly. |
