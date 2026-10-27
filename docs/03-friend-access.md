# 03 · How friends connect (the Tailscale user limit)

**Today:** friends join your tailnet and connect to `<tailscale-ip>:<port>`. The free plan caps how many users can be in your tailnet, and you're close to it.

**Check first:** Tailscale's free **Personal** plan was raised from 3 to **6 users** (reported as April 2026). If your count is from before that, you may have more room than you think. Look at the admin console → **Users** and **Billing**.

Basecamp doesn't depend on which option you pick: each server has a **join address**, and the copy button uses it. Picking a relay later just changes that address.

## Options

### A. Tailscale device sharing (try this first)

Instead of inviting friends **into** your tailnet, you **share the prod PC** with them. Each friend makes their own free Tailscale account (their own tailnet), you send a share link for the prod PC, and it shows up in their device list. They connect to the same `<tailscale-ip>:<port>`.

- ✅ No new software, nothing exposed to the internet, same address as today.
- ✅ Tailscale's FAQ says devices shared *into* a tailnet don't count against *that* tailnet's limits; shared-in users show as external in yours.
- ⚠️ **Not confirmed** whether people you share a device with count toward **your** user limit. Test with one friend: share the device, then check whether your user count went up. If it didn't, this solves the problem completely.
- ⚠️ Your tailnet policy must allow shared users to reach the game ports (and port 443 if they should see the Basecamp friend view).
- Friends currently in your tailnet would move over one at a time (remove from tailnet, send share).

### B. playit.gg (public relay, no Tailscale for friends)

A small agent on the prod PC makes an **outbound** tunnel to playit.gg, which hands out a public address (e.g. `something.at.ply.gg:12345`) that forwards to your game port. Friends need nothing installed.

- ✅ Works with UDP games, no router port forward, no friend limit. Free tier allows a few tunnels (sources say 3-4 TCP + 3-4 UDP; check their site); paid is about $3/month.
- ⚠️ The game server is now **on the public internet**. Anyone with the address can try to join, so every server needs a game password, and the game's own server code is now attack surface.
- ⚠️ Players' IPs show as playit's, so in-game IP bans don't work; some games add a little latency through the relay.
- ⚠️ The agent is a new program on the prod PC: its own folder and its own firewall rules (see [04-deployment.md](04-deployment.md)). Going public is also the point to reconsider running game servers under a separate Windows user (D-11).
- Basecamp could later show the playit address as the join address and even manage the agent.

### C. Your own relay on a cheap VPS

A $4-6/month VPS runs WireGuard or `frp`; the prod PC connects out to it, the VPS forwards game ports. Same pros/cons as B, but you control it (firewall by country, logs), and more setup and upkeep.

### D. Headscale (self-hosted Tailscale control server)

No user limit, friends keep using Tailscale clients. But it needs a public server for the control plane (a VPS again) and you become the Tailscale admin for everyone. Big hammer for this problem.

### E. Router port forwarding

**Ruled out:** no router forwards or UPnP on the host PC ([04-deployment.md](04-deployment.md)), and it would expose your real home IP.

### F. Paid Tailscale

Starter plans are per-user per month; with many friends it adds up fast. Listed for completeness.

## Recommendation

1. Check your real user count against the 6-user limit.
2. Try **A** with one friend and see whether the count moves. If not, move everyone to device sharing. Nothing in Basecamp changes.
3. If A doesn't work, go to **B** for games where friends of friends join, with a game password on every server. Add "playit address" support to Basecamp then (it's on the Later list).

## Sources

- [Tailscale pricing](https://tailscale.com/pricing/) and [pricing FAQ](https://tailscale.com/pricing/faq)
- [Report of the free tier going to 6 users](https://thisisanitsupportgroup.beehiiv.com/p/tailscale-s-free-tier-just-doubled-if-you-ve-been-putting-it-off-today-s-the-morning)
- [playit.gg alternatives roundup (tunnel counts)](https://pinggy.io/blog/best_playit_gg_alternatives/), [playit.gg traffic limits thread](https://discuss.playit.gg/t/traffic-limits/5574), [playit.gg setup walkthrough](https://www.teklynk.com/blog/how-to-host-a-game-server-from-your-home-using-playItgg)
