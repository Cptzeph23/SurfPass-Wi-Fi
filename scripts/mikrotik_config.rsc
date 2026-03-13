# ─────────────────────────────────────────────────────────────────────────────
# SurfPass WiFi - MikroTik RouterOS Configuration
# Tested on RouterOS 6.49+ and 7.x
# ─────────────────────────────────────────────────────────────────────────────

# ══════════════════════════════════════════════════════════════════════════════
# 1. BRIDGE & INTERFACE SETUP
# ══════════════════════════════════════════════════════════════════════════════

# Create hotspot bridge
/interface bridge add name=bridge-hotspot

# Add WiFi interface to bridge
/interface bridge port
  add bridge=bridge-hotspot interface=wlan1

# Assign IP to bridge (gateway for clients)
/ip address
  add address=192.168.100.1/24 interface=bridge-hotspot


# ══════════════════════════════════════════════════════════════════════════════
# 2. WIRELESS CONFIGURATION (Open Network - No Password)
# ══════════════════════════════════════════════════════════════════════════════

/interface wireless
  set wlan1 \
    ssid="SurfPass WiFi" \
    mode=ap-bridge \
    band=2ghz-b/g/n \
    channel-width=20/40mhz-Ce \
    frequency=auto \
    security-profile=default \
    disabled=no

# Security profile: OPEN (no WPA)
/interface wireless security-profiles
  set [find name=default] \
    mode=none \
    name=default


# ══════════════════════════════════════════════════════════════════════════════
# 3. DHCP SERVER
# ══════════════════════════════════════════════════════════════════════════════

/ip pool
  add name=hotspot-pool ranges=192.168.100.10-192.168.100.254

/ip dhcp-server
  add address-pool=hotspot-pool \
      disabled=no \
      interface=bridge-hotspot \
      name=hotspot-dhcp \
      lease-time=1h

/ip dhcp-server network
  add address=192.168.100.0/24 \
      dns-server=8.8.8.8,1.1.1.1 \
      gateway=192.168.100.1


# ══════════════════════════════════════════════════════════════════════════════
# 4. HOTSPOT SETUP (Captive Portal Redirect)
# ══════════════════════════════════════════════════════════════════════════════

/ip hotspot profile
  add \
    name=surfpass-profile \
    hotspot-address=192.168.100.1 \
    login-by=mac \
    http-cookie-lifetime=1d \
    html-directory=hotspot \
    use-radius=no

# Create hotspot on bridge
/ip hotspot
  add \
    name=hotspot1 \
    interface=bridge-hotspot \
    address-pool=hotspot-pool \
    profile=surfpass-profile \
    disabled=no

# Hotspot user profile (for authenticated users)
/ip hotspot user profile
  add \
    name=surfpass-users \
    idle-timeout=none \
    keepalive-timeout=2m \
    shared-users=1

# ══════════════════════════════════════════════════════════════════════════════
# 5. WALLED GARDEN (Allow portal access without authentication)
# Allow access to captive portal server without payment
# ══════════════════════════════════════════════════════════════════════════════

/ip hotspot walled-garden
  # Allow access to portal API server
  add dst-host=192.168.100.100 action=allow
  add dst-host=192.168.100.100:80 action=allow
  add dst-host=192.168.100.100:443 action=allow

  # Allow M-Pesa callback domain (Safaricom)
  add dst-host=*.safaricom.co.ke action=allow
  add dst-host=api.safaricom.co.ke action=allow
  add dst-host=sandbox.safaricom.co.ke action=allow

/ip hotspot walled-garden ip
  # Allow portal server IP directly
  add dst-address=192.168.100.100 action=accept
  add dst-address=8.8.8.8 action=accept  # DNS


# ══════════════════════════════════════════════════════════════════════════════
# 6. FIREWALL RULES
# ══════════════════════════════════════════════════════════════════════════════

/ip firewall filter
  # Allow established/related
  add chain=forward connection-state=established,related action=accept comment="Allow established"

  # Allow hotspot clients to portal server
  add chain=forward in-interface=bridge-hotspot \
      dst-address=192.168.100.100 action=accept \
      comment="Hotspot to portal server"

  # Allow authenticated hotspot users internet access
  add chain=forward in-interface=bridge-hotspot \
      src-address-list=hotspot-auth action=accept \
      comment="Authenticated hotspot users"

  # Drop all other hotspot traffic (unauthenticated)
  add chain=forward in-interface=bridge-hotspot action=drop \
      comment="Drop unauthenticated hotspot"

# NAT for internet access
/ip firewall nat
  add chain=srcnat out-interface=ether1 action=masquerade \
      comment="NAT to WAN"


# ══════════════════════════════════════════════════════════════════════════════
# 7. HOTSPOT REDIRECT (Custom Portal URL)
# Point hotspot redirect to your captive portal server
# ══════════════════════════════════════════════════════════════════════════════

# The hotspot profile handles redirect automatically.
# Ensure your portal server is at 192.168.100.100
# Update hotspot profile login page to point to your portal:

/ip hotspot profile
  set surfpass-profile \
    login-by=mac \
    html-directory=flash/hotspot \
    http-proxy=192.168.100.100:80


# ══════════════════════════════════════════════════════════════════════════════
# 8. API ACCESS (for SurfPass backend to control router)
# ══════════════════════════════════════════════════════════════════════════════

# Create dedicated API user (restrict to portal server IP)
/user
  add name=surfpass-api \
      password=CHANGE_ME_STRONG_PASSWORD \
      group=full \
      address=192.168.100.100/32 \
      comment="SurfPass API user"

# Enable API service (port 8728)
/ip service
  set api disabled=no port=8728 address=192.168.100.100/32
  set api-ssl disabled=no port=8729 address=192.168.100.100/32
  # Disable unused services for security
  set telnet disabled=yes
  set ftp disabled=yes
  set www-ssl disabled=no


# ══════════════════════════════════════════════════════════════════════════════
# 9. BANDWIDTH CONTROL (Optional Queue Tree)
# ══════════════════════════════════════════════════════════════════════════════

# Global queue for hotspot interface
/queue tree
  add name=hotspot-download \
      parent=bridge-hotspot \
      max-limit=100M \
      comment="Total hotspot download limit"


# ══════════════════════════════════════════════════════════════════════════════
# 10. VERIFY CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════
# Run these commands to verify:
#   /ip hotspot print
#   /ip hotspot active print
#   /ip hotspot user print
#   /interface wireless print