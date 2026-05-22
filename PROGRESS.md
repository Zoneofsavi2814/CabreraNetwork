# Network Speed Goal Progress

## Current Milestone

- Milestone: Final review
- Status: Completed local optimizations; router/device throttling requires explicit approval

## Files Changed

- `PROGRESS.md` - goal tracking log

## Commands Run

- `networksetup -getcurrentlocation`
- `networksetup -listlocations`
- `networksetup -listallhardwareports`
- `networksetup -listallnetworkservices`
- `route -n get default`
- `scutil --dns`
- `ifconfig`
- `networksetup -getdnsservers Wi-Fi`
- `networksetup -getsearchdomains Wi-Fi`
- `networksetup -getMTU Wi-Fi`
- `networksetup -getwebproxy Wi-Fi`
- `networksetup -getsecurewebproxy Wi-Fi`
- `networksetup -getautoproxyurl Wi-Fi`
- `system_profiler SPAirPortDataType`
- `ping -c 20 -i 0.2 -n 192.168.0.1`
- `ping -c 20 -i 0.2 -n 192.168.0.101`
- `ping -c 20 -i 0.2 -n 1.1.1.1`
- `ping -c 20 -i 0.2 -n 8.8.8.8`
- `dig +time=2 +tries=1 @192.168.0.101 cloudflare.com A`
- `dig +time=2 +tries=1 @192.168.0.1 cloudflare.com A`
- `dig +time=2 +tries=1 @1.1.1.1 cloudflare.com A`
- `dig +time=2 +tries=1 @8.8.8.8 cloudflare.com A`
- `ping -D -s 1472 -c 3 -n 1.1.1.1`
- `ping -D -s 1464 -c 3 -n 1.1.1.1`
- `ping -D -s 1452 -c 3 -n 1.1.1.1`
- `ping -D -s 1400 -c 3 -n 1.1.1.1`
- `dscacheutil -q host -a name pi4.local`
- `networksetup -listpreferredwirelessnetworks en1`
- `profiles list -type configuration`
- `networkQuality -v`
- `nettop -P -L 1 -x -m tcp -J bytes_in,bytes_out`
- `networksetup -removepreferredwirelessnetwork en1 ...`
- `networksetup -setdnsservers Wi-Fi empty`
- `networksetup -ordernetworkservices Ethernet Wi-Fi "Thunderbolt Bridge" "USB JTAG/serial debug unit" "USB Serial"`
- `dscacheutil -flushcache`
- `ssh pi4@192.168.0.101` read of `/home/pi4/.pi4-noc-router-topology.json`

## Validation Results

- Active route: Wi-Fi `en1` through gateway `192.168.0.1`; Ethernet `en0` is inactive.
- Wi-Fi baseline: 802.11ax on 5 GHz channel 157, 80 MHz; signal/noise `-49 / -90 dBm`, transmit rate `907 Mbps`, MCS `9`.
- MTU: `1500` works; DF pings at size `1472` reached `1.1.1.1` with `0%` packet loss.
- Packet loss baseline: gateway, Pi4, `1.1.1.1`, and `8.8.8.8` all showed `0%` loss.
- Baseline gateway latency: `min/avg/max/stddev = 4.557/52.384/195.797/53.401 ms`.
- After gateway latency: `min/avg/max/stddev = 4.471/14.074/91.584/21.723 ms`.
- Baseline `1.1.1.1` latency: `19.188/48.309/103.522/28.520 ms`.
- After `1.1.1.1` latency: `15.686/30.413/101.499/24.940 ms`.
- Baseline `8.8.8.8` latency: `11.639/32.518/101.181/24.548 ms`.
- After `8.8.8.8` latency: `12.866/33.055/109.361/27.710 ms`.
- Baseline DNS: AdGuard `192.168.0.101` was fastest at about `7-12 ms`; router fallback was usually `20-25 ms` with one `103 ms` spike.
- After DNS: DHCP now supplies only `192.168.0.101` and `192.168.0.1`; AdGuard remained responsive but had two spikes (`72 ms`, `92 ms`) during the after sample.
- mDNS: `pi4.local` resolved to `192.168.0.101` and IPv6 local address before and after cache flush.
- Baseline `networkQuality`: down `190.325 Mbps`, up `199.083 Mbps`, idle latency `32.992 ms`, loaded responsiveness `172.106 ms`.
- After `networkQuality`: down `222.047 Mbps`, up `165.935 Mbps`, idle latency `30.424 ms`, loaded responsiveness `197.251 ms`.
- Router cache: this Mac is on the loft AP over `5g`; `LGwebOSTV` on the loft AP over `2.4g` repeatedly reported extremely high receive traffic during validation.

## Blockers / Notes

- Existing repo worktree had unrelated modified files before this goal started: `MEMORY.md`, `README.md`, `index.html`, `src/App.jsx`, `src/styles.css`.
- GRID notes identify the home gateway as `192.168.0.1`, Pi4/AdGuard as `192.168.0.101`, basement AP as `192.168.0.117`, and loft AP as `192.168.0.176`.
- Removed stale preferred Wi-Fi networks from this Mac: `AEROGUEST`, `SM-G930PF07`, `iPhone`, `MySpectrumWiFi58-5G`, `Janice iPhone`, `TP-Link_F7E4`, and `3373_24A`. `SpeedForce` remains.
- Changed Wi-Fi DNS from manual `192.168.0.101`, `192.168.0.1`, `1.1.1.1`, `8.8.8.8` to DHCP-supplied DNS. Current effective DNS is `192.168.0.101`, `192.168.0.1`.
- Changed network service order from USB/debug services first to `Ethernet`, `Wi-Fi`, `Thunderbolt Bridge`, then USB serial/debug services.
- Flushed the local DNS cache with `dscacheutil -flushcache`.
- No local Mac process was killed; `nettop` showed Codex/Safari/WebKit/SSH activity but no unrelated obvious background hog.
- Did not block or throttle `LGwebOSTV` because that would affect another device on the LAN and needs explicit approval.
- Remaining likely bottleneck: Wi-Fi/AP airtime and loaded latency/bufferbloat. Ethernet or router QoS/SQM would be the next higher-impact fix.

## Next Action

- Follow-up options: wire the Mac mini to Ethernet, move/force it to the 6 GHz band if available, enable router QoS/SQM or device priority for the Mac, or pause/throttle the loft TV client during latency-sensitive work.

## 2026-05-18 Loaded Latency Follow-Up

### Context

- User disabled Wi-Fi on `LGwebOSTV`, which had previously appeared as a very high-traffic loft AP client.
- Router cache after that change no longer showed `LGwebOSTV` as a top talker.
- Mac mini remained on the loft AP over `5g`.
- Current Wi-Fi readout: 802.11ax, channel `157` on `5 GHz`, `80 MHz`; signal/noise about `-50 / -89 dBm`; transmit rate about `960 Mbps`.

### Idle Measurements After LG Wi-Fi Disable

- Loft AP `192.168.0.176`: `0%` loss, `3.565/5.324/19.581/2.413 ms`.
- Gateway `192.168.0.1`: `0%` loss, `4.309/6.416/22.982/2.798 ms`.
- Cloudflare `1.1.1.1`: `0%` loss, `14.220/19.629/25.063/2.223 ms`.
- DNS: AdGuard `192.168.0.101` stayed around `6-11 ms`; router DNS `192.168.0.1` stayed around `12-37 ms`.

### Loaded Measurements

- Combined upload/download `networkQuality`: down `264.803 Mbps`, up `214.609 Mbps`, idle latency `29.627 ms`, loaded responsiveness `173.055 ms`, HTTP loaded `210.760 ms`.
- During combined load:
  - Loft AP ping: `0%` loss, `2.617/41.001/163.187/40.560 ms`.
  - Gateway ping: `0%` loss, `3.945/40.539/170.713/40.545 ms`.
  - Cloudflare ping: `0%` loss, `14.932/63.211/202.983/48.742 ms`.
- Download-only `networkQuality`: down `482.501 Mbps`, downlink responsiveness `66.745 ms`, HTTP loaded `116.804 ms`.
- During download-only load:
  - Loft AP ping: `0%` loss, `2.282/21.743/100.077/24.964 ms`.
  - Gateway ping: `0%` loss, `3.677/23.851/108.673/26.638 ms`.
  - Cloudflare ping: `0%` loss, `13.832/41.354/147.749/30.651 ms`.
- Upload-only `networkQuality`: up `286.537 Mbps`, uplink responsiveness `139.346 ms`, HTTP loaded `236.789 ms`.
- During upload-only load:
  - Loft AP ping: `0%` loss, `2.543/7.302/39.364/4.939 ms`.
  - Gateway ping: `0%` loss, `3.107/8.826/39.413/5.463 ms`.
  - Cloudflare ping: `0%` loss, `10.947/34.236/115.394/14.985 ms`.

### Interpretation

- Idle path is healthy after the LG Wi-Fi change.
- Download and combined load inflate latency all the way back to the loft AP/gateway, so part of the loaded-latency problem is local Wi-Fi airtime/queueing.
- Upload-only does not significantly inflate AP/gateway ICMP latency, but still reports poor HTTP loaded latency, so part of the Apple `networkQuality` score is likely WAN/application queueing rather than raw local packet loss.
- TP-Link readback showed device priority disabled (`enablePriority: False`) and speed limits off (`enableLimit: off`) for devices, including the Mac. No router state was changed.
