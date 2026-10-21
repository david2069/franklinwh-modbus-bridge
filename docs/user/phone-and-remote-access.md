# Your phone, notifications and access from away

This page explains three things. You don't need any technical knowledge.

1. How to get the bridge's alerts on your phone, with the **Home Assistant Companion app**.
2. How to put the bridge on your phone's home screen, like an app.
3. How to reach it when you're away from home, and which way to choose.

!!! note "Checked 2026-10-21"
    Product names, prices and steps change. Each section links to the official page; if
    something here differs from it, the official page is right.

## 1. Alerts on your phone: the Home Assistant Companion app

The bridge sends its alerts through Home Assistant (HA).

- **Without the Companion app,** alerts still appear in HA as notifications (the bell in
  HA's sidebar). You see them when you open HA.
- **With the Companion app,** they also arrive on your phone as a push notification, even
  when HA isn't open.

**Set it up:**

1. Install **Home Assistant** from the App Store (iPhone/iPad) or Google Play (Android). It
   is free. See [companion.home-assistant.io](https://companion.home-assistant.io/).
2. Open it and sign in to your Home Assistant.
3. When it asks, **allow notifications**. This step matters: without it the phone can't be
   chosen in the bridge.
4. In HA, the phone now appears as a notification target named
   `notify.mobile_app_<your phone's name>`, for example `notify.mobile_app_pixel_8`.
5. In the bridge, pick that phone as the notify target. Today that is a schedule's
   **Notify** action (see [Home Assistant setup](ha-setup.md)). The bridge's own critical
   alerts (a refused battery command, live data missing, the aGate unreachable) are being
   added; when they ship, you choose the phone for them in the bridge's settings.

Nothing arrives? On the phone, check the Companion app's notification permission in the
phone's settings, then send the test again.

## 2. The bridge on your home screen

Open the bridge **through Home Assistant**: the bridge in HA's sidebar, or its
**Open Web UI** button. Then save that page:

=== "iPhone / iPad (Safari)"

    1. Open the bridge page in **Safari**.
    2. Tap **Share** (the square with an arrow).
    3. Tap **Add to Home Screen**, then **Add**.

=== "Android (Chrome)"

    1. Open the bridge page in **Chrome**.
    2. Tap **⋮** (top right).
    3. Tap **Add to Home screen** (or **Install app**), then **Add**.

!!! tip "Or use the Companion app itself"
    The Companion app shows HA's sidebar, including the bridge. That works from home and,
    with remote access below, from anywhere.

!!! note "Use Home Assistant's address, not the bridge's own port"
    Save the page you reach **through Home Assistant**. In the add-on the bridge's own
    port (8099) is off by default; if you turn it on, it is for your home network only.
    Reaching the bridge directly from a phone away from home would need its own TLS
    set-up, so the Home Assistant address is simpler and safer.

    Want a simpler screen for the home screen? The bridge's
    [simple dashboard](ui/user-dashboard.md) shows the essentials at a glance: open it,
    then save it the same way.

## 3. Reaching it away from home

At home, your phone reaches Home Assistant directly. Away from home it needs a safe way
in. **Never forward a port on your router to the bridge or to Home Assistant.**

There are three common ways. You can install more than one: a second one is a useful
backup if the first is ever down.

### We recommend Home Assistant Link (Nabu Casa)

Home Assistant's own remote access service, run by Nabu Casa. It's the simplest to set
up, and in Nabu Casa's words your subscription "directly funds the full-time development
of Home Assistant". Think of it as a monthly donation that also gets you remote access.

!!! info "New name"
    Home Assistant **Cloud** is renamed Home Assistant **Link** from HA 2026.12
    (2 December 2026). Until then, HA's screens still say "Home Assistant Cloud". It is the
    same subscription, and you don't need to do anything.
    ([Nabu Casa's announcement](https://www.nabucasa.com/news/2026-10-02-cloud-becomes-link/))

### Comparison

| | Home Assistant Link (Nabu Casa) | Tailscale (app for HA) | Cloudflare Tunnel (cloudflared app for HA) |
|---|---|---|---|
| **Installation** | In HA: **Settings → Home Assistant Cloud** (Link from 2026.12) → start the trial and sign in. No add-on, no router changes. | Create a Tailscale account. Install the **Tailscale** app from the Home Assistant Community Apps store and sign in. Install **Tailscale** on each phone and computer too. | You need your **own domain** using Cloudflare for DNS. Add the cloudflared app's repository, install it, sign in to Cloudflare, and set HA's reverse-proxy settings (**Trust X-Forwarded-For**, trusted proxy `172.30.33.0/24`). |
| **Cost** | US$6.50 a month or US$65 a year (tax extra). 31-day free trial, no card needed. [Pricing](https://www.nabucasa.com/pricing/) | Personal plan free for up to 6 users, non-commercial use. [Pricing](https://tailscale.com/pricing) | The tunnel is free on Cloudflare's free plan. You pay for the domain name each year. [Cloudflare plans](https://www.cloudflare.com/plans/) |
| **Best for** | Most people. Families, and phones that just need to work anywhere. | People who want access to stay private to their own devices. | People who already run their own domain on Cloudflare. |
| **Who can reach your HA** | Anyone with your HA address, who then has to log in to HA. | **Only your own devices** signed in to your Tailscale network (unless you turn on Funnel). | **The whole internet** at your domain, who then has to log in to HA. Cloudflare Access (Zero Trust) can add a second login in front. |
| **Strengths** | Simplest; no domain, no router changes; works with the Companion app straight away; also includes offsite backups, Alexa/Google Home and voice. **Supports Home Assistant.** | Free; nothing exposed to the internet; also reaches other devices at home if you choose. | Free tunnel; your own address; no router changes. The same Cloudflare account can also hold your HA backups (R2, below). |
| **Weaknesses** | A paid subscription. | Every phone and computer needs the Tailscale app, and family members need accounts. | Needs a domain and more set-up; exposes HA publicly, so use strong HA logins with multi-factor authentication. |

### Install links

The buttons below open **your own** Home Assistant (via my.home-assistant.io) at the right
page. It asks for your HA address the first time.

| | Project | Add to your Home Assistant |
|---|---|---|
| **Home Assistant Link** | [nabucasa.com](https://www.nabucasa.com/) | Built in: **Settings → Home Assistant Cloud** (Link from 2026.12). Nothing to install. |
| **Tailscale** | [hassio-addons/app-tailscale](https://github.com/hassio-addons/app-tailscale) | [Open the Tailscale app in your HA](https://my.home-assistant.io/redirect/supervisor_addon/?addon=a0d7b954_tailscale&repository_url=https%3A%2F%2Fgithub.com%2Fhassio-addons%2Frepository) |
| **Cloudflare Tunnel (cloudflared)** | [homeassistant-apps/app-cloudflared](https://github.com/homeassistant-apps/app-cloudflared) | [Add the cloudflared repository to your HA](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fhomeassistant-apps%2Frepository), then install **Cloudflared** from the store. |

**Whichever you use:** turn on multi-factor authentication for your Home Assistant login
(**your profile → Security**). Keep Home Assistant updated.

### Backups off the premises

Keep a copy of your Home Assistant backups somewhere other than your home:

- **Home Assistant Link** includes offsite backups.
- **Cloudflare R2:** Home Assistant's
  [Cloudflare R2 integration](https://www.home-assistant.io/integrations/cloudflare_r2/)
  stores backups in an R2 bucket. R2 is free up to **10 GB of storage a month**, with no
  download (egress) charges; above that it's charged
  ([R2 pricing](https://developers.cloudflare.com/r2/pricing/)). Keep the number of
  backups you retain small enough to stay under 10 GB.

  It needs Home Assistant 2026.2 or later. Set it up in three steps:

  1. In the Cloudflare dashboard, **Storage & databases → R2 object storage**: create a
     bucket (Standard storage).
  2. **Manage API Tokens → Create User API token:** tick **Object Read & Write**, choose
     **Apply to specific buckets only** with your bucket. Note the Access key ID, Secret
     access key and the endpoint URL.
  3. [Add the Cloudflare R2 integration to your HA](https://my.home-assistant.io/redirect/config_flow_start?domain=cloudflare_r2)
     and enter those values and the bucket name (a folder prefix is optional). R2 then
     appears as a backup location in **Settings → System → Backups**.

### Use two

Install Home Assistant Link as your main way in, and Tailscale as a free backup. If one
is ever unavailable, the other still gets you in.

## Something wrong or unclear?

Ask in [Discussions](https://github.com/david2069/franklinwh-modbus-bridge/discussions).
Everyone is welcome, technical or not.
