# Copyright (c) 2019, Mathy Vanhoef <mathy.vanhoef@nyu.edu>
#
# This code may be distributed under the terms of the BSD license.
# See README for more details.
from scapy.all import *
from Crypto.Cipher import AES
from datetime import datetime
import binascii

#### Constants ####

IEEE_TLV_TYPE_BEACON = 0

WLAN_REASON_DISASSOC_DUE_TO_INACTIVITY = 4
WLAN_REASON_CLASS2_FRAME_FROM_NONAUTH_STA = 6
WLAN_REASON_CLASS3_FRAME_FROM_NONASSOC_STA = 7

#### Basic output and logging functionality ####

ALL, DEBUG, INFO, STATUS, WARNING, ERROR = range(6)
COLORCODES = { "gray"  : "\033[0;37m",
               "green" : "\033[0;32m",
               "orange": "\033[0;33m",
               "red"   : "\033[0;31m" }

global_log_level = INFO
def log(level, msg, color=None, showtime=True):
	if level < global_log_level: return
	if level == DEBUG   and color is None: color="gray"
	if level == WARNING and color is None: color="orange"
	if level == ERROR   and color is None: color="red"
	msg = (datetime.now().strftime('[%H:%M:%S] ') if showtime else " "*11) + COLORCODES.get(color, "") + msg + "\033[1;0m"
	print(msg)

def change_log_level(delta):
	global global_log_level
	global_log_level += delta

#### Back-wards compatibility with older scapy

if not "Dot11FCS" in locals():
	class Dot11FCS():
		pass
if not "Dot11Encrypted" in locals():
	class Dot11Encrypted():
		pass
	class Dot11CCMP():
		pass
	class Dot11TKIP():
		pass

#### Linux ####

def get_device_driver(iface):
	path = "/sys/class/net/%s/device/driver" % iface
	try:
		output = subprocess.check_output(["readlink", "-f", path])
		return output.decode('utf-8').strip().split("/")[-1]
	except:
		return None

#### Utility ####

def get_mac_address(interface):
	return open("/sys/class/net/%s/address" % interface).read().strip()

def addr2bin(addr):
	return binascii.a2b_hex(addr.replace(':', ''))

def get_channel(iface):
	output = str(subprocess.check_output(["iw", iface, "info"]))
	p = re.compile("channel (\d+)")
	m = p.search(output)
	if m == None: return None
	return int(m.group(1))

def get_channel(iface):
	output = str(subprocess.check_output(["iw", iface, "info"]))
	p = re.compile("channel (\d+)")
	return int(p.search(output).group(1))

def set_channel(iface, channel):
	subprocess.check_output(["iw", iface, "set", "channel", str(channel)])

def set_macaddress(iface, macaddr):
	subprocess.check_output(["ifconfig", iface, "down"])
	subprocess.check_output(["macchanger", "-m", macaddr, iface])

def get_macaddress(iface):
	"""This works even for interfaces in monitor mode."""
	s = get_if_raw_hwaddr(iface)[1]
	return ("%02x:" * 6)[:-1] % tuple(orb(x) for x in s)

def get_iface_type(iface):
	output = str(subprocess.check_output(["iw", iface, "info"]))
	p = re.compile("type (\w+)")
	return str(p.search(output).group(1))

def set_monitor_mode(iface):
	# Note: we let the user put the device in monitor mode, such that they can control optional
	#       parameters such as "iw wlan0 set monitor active" for devices that support it.
	if get_iface_type(iface) != "monitor":
		# Some kernels (Debian jessie - 3.16.0-4-amd64) don't properly add the monitor interface. The following ugly
		# sequence of commands assures the virtual interface is properly registered as a 802.11 monitor interface.
		subprocess.check_output(["ifconfig", iface, "down"])
		subprocess.check_output(["iw", iface, "set", "type", "monitor"])
		time.sleep(0.5)
		subprocess.check_output(["iw", iface, "set", "type", "monitor"])

	subprocess.check_output(["ifconfig", iface, "up"])
	subprocess.check_output(["ifconfig", iface, "mtu", "2200"])

#### Injection Tests ####

def get_nearby_ap_addr(sin):
	# If this interface itself is also hosting an AP, the beacons transmitted by it might be
	# returned as well. We filter these out by the condition `p.dBm_AntSignal != None`.
	beacons = list(sniff(opened_socket=sin, timeout=0.5, lfilter=lambda p: (Dot11 in p or Dot11FCS in p) \
									and p.type == 0 and p.subtype == 8 \
									and p.dBm_AntSignal != None))
	if len(beacons) == 0:
		return None, None
	beacons.sort(key=lambda p: p.dBm_AntSignal, reverse=True)
	return beacons[0].addr2, get_ssid(beacons[0])

def inject_and_capture(sout, sin, p, count=0):
	# Append unique label to recognize injected frame
	label = b"AAAA" + struct.pack(">II", random.randint(0, 2**32), random.randint(0, 2**32))
	toinject = p/Raw(label)
	log(DEBUG, "Injecting test frame: " + repr(toinject))
	sout.send(RadioTap()/toinject)

	# TODO:Move this to a shared socket interface?
	# Note: this workaround for Intel is only needed if the fragmented frame is injected using
	#       valid MAC addresses. But for simplicity just execute it after any fragmented frame.
	if sout.intel_mf_workaround and toinject.FCfield & Dot11(FCfield="MF").FCfield != 0:
		sout.send(RadioTap()/Dot11())
		log(DEBUG, "Sending dummy frame after injecting frame with MF flag set")

	# 1. When using a 2nd interface: capture the actual packet that was injected in the air.
	# 2. Not using 2nd interface: capture the "reflected" frame sent back by the kernel. This allows
	#    us to at least detect if the kernel (and perhaps driver) is overwriting fields. It generally
	#    doesn't allow us to detect if the device/firmware itself is overwriting fields.
	packets = sniff(opened_socket=sin, timeout=1, count=count, lfilter=lambda p: p != None and label in raw(p))

	return packets

def test_packet_injection(sout, sin, p, test_func=None):
	"""Check if given property holds of all injected frames"""
	packets = inject_and_capture(sout, sin, p, count=1)
	if len(packets) < 1:
		raise IOError("Unable to inject test frame. Could be due to background noise or channel/driver/device/..")
	return all([test_func(cap) for cap in packets])

def test_injection_fields(sout, sin, ref, strtype):
	bad_inject = False

	p = Dot11(FCfield=ref.FCfield, addr1=ref.addr1, addr2=ref.addr2, addr3=ref.addr3, type=2, SC=30<<4)/LLC()/SNAP()/EAPOL()/EAP()
	if not test_packet_injection(sout, sin, p, lambda cap: EAPOL in cap):
		log(STATUS, "    Unable to inject EAPOL frames!")
		bad_inject = True

	p = Dot11(FCfield=ref.FCfield, addr1=ref.addr1, addr2=ref.addr2, addr3=ref.addr3, type=2, SC=30<<4)
	if not test_packet_injection(sout, sin, p, lambda cap: cap.SC == p.SC):
		log(STATUS, "    Sequence number of injected frames is being overwritten!")
		bad_inject = True

	p = Dot11(FCfield=ref.FCfield, addr1=ref.addr1, addr2=ref.addr2, addr3=ref.addr3, type=2, SC=(30<<4)|1)
	if not test_packet_injection(sout, sin, p, lambda cap: (cap.SC & 0xf) == 1):
		log(STATUS, "    Fragment number of injected frames is being overwritten!")
		bad_inject = True

	p = Dot11(FCfield=ref.FCfield, addr1=ref.addr1, addr2=ref.addr2, addr3=ref.addr3, type=2, subtype=8, SC=30<<4)/Dot11QoS(TID=2)
	if not test_packet_injection(sout, sin, p, lambda cap: cap.TID == p.TID):
		log(STATUS, "    QoS TID of injected frames is being overwritten!")
		bad_inject = True

	if bad_inject:
		log(ERROR, f"[-] Some fields are overwritten when injected using {strtype}.")
	else:
		log(STATUS, f"[+] All tested fields are properly injected when using {strtype}.", color="green")

def test_injection_order(sout, sin, ref, strtype):
	label = b"AAAA" + struct.pack(">II", random.randint(0, 2**32), random.randint(0, 2**32))
	p2 = Dot11(FCfield=ref.FCfield, addr1=ref.addr1, addr2=ref.addr2, type=2, subtype=8, SC=33)/Dot11QoS(TID=2)
	p6 = Dot11(FCfield=ref.FCfield, addr1=ref.addr1, addr2=ref.addr2, type=2, subtype=8, SC=33)/Dot11QoS(TID=6)

	# First frame causes Tx queue to be busy. Next two frames tests if frames are reordered.
	for p in [p2, p2, p2, p6]:
		sout.send(RadioTap()/p/Raw(label))

	packets = sniff(opened_socket=sin, timeout=1.5, lfilter=lambda p: Dot11QoS in p and label in raw(p))
	tids = [p[Dot11QoS].TID for p in packets]
	log(STATUS, "Captured TIDs: " + str(tids))

	# Sanity check the captured TIDs, and then analyze the results
	if not (2 in tids and 6 in tids):
		log(ERROR, f"[-] We didn't capture all injected QoS TID frames. Please restart the test.")
	elif tids != sorted(tids):
		log(ERROR, f"[-] Frames with different QoS TIDs are reordered during injection with {strtype}.")
	else:
		log(STATUS, f"[+] Frames with different QoS TIDs are not reordered during injection with {strtype}.", color="green")

def test_injection_fragment(sout, sin, ref):
	p = Dot11(FCfield=ref.FCfield, addr1=ref.addr1, addr2=ref.addr2, type=2, subtype=8, SC=33<<4)
	p = p/Dot11QoS(TID=2)/LLC()/SNAP()/EAPOL()/EAP()
	p.FCfield |= Dot11(FCfield="MF").FCfield
	captured = inject_and_capture(sout, sin, p, count=1)
	if len(captured) == 0:
		log(ERROR, "[-] Unable to inject fragmented frame using (partly) valid MAC addresses. Other tests might fail too.")
	else:
		log(STATUS, "[+] Fragmented frames using (partly) valid MAC addresses can be injected.", color="green")

def test_injection_ack(sout, sin, addr1, addr2):
	suspicious = False
	test_fail = False

	# Test number of retransmissions
	p = Dot11(addr1="00:11:00:00:02:01", addr2="00:11:00:00:02:01", type=2, SC=33<<4)
	num = len(inject_and_capture(sout, sin, p))
	log(STATUS, f"Injected frames seem to be (re)transitted {num} times")
	if num == 0:
		log(ERROR, "Couldn't capture injected frame. Please restart the test.")
		test_fail = True
	elif num == 1:
		log(WARNING, "Injected frames don't seem to be retransmitted!")
		suspicious = True

	# Test ACK towards an unassigned MAC address
	p = Dot11(FCfield="to-DS", addr1=addr1, addr2="00:22:00:00:00:01", type=2, SC=33<<4)
	num = len(inject_and_capture(sout, sin, p))
	log(STATUS, f"Captured {num} (re)transmitted frames to the AP when using a spoofed sender address")
	if num == 0:
		log(ERROR, "Couldn't capture injected frame. Please restart the test.")
		test_fail = True
	if num > 2:
		log(STATUS, "  => Acknowledged frames with a spoofed sender address are still retransmitted. This has low impact.")

	# Test ACK towards an assigned MAC address
	p = Dot11(FCfield="to-DS", addr1=addr1, addr2=addr2, type=2, SC=33<<4)
	num = len(inject_and_capture(sout, sin, p))
	log(STATUS, f"Captured {num} (re)transmitted frames to the AP when using the real sender address")
	if num == 0:
		log(ERROR, "Couldn't capture injected frame. Please restart the test.")
		test_fail = True
	elif num > 2:
		log(STATUS, "  => Acknowledged frames with real sender address are still retransmitted. This might impact time-sensitive tests.")
		suspicious = True

	if suspicious:
		log(WARNING, "[-] Retransmission behaviour isn't ideal. This test can be unreliable (e.g. due to background noise).")
	elif not test_fail:
		log(STATUS, "[+] Retransmission behaviour is good. This test can be unreliable (e.g. due to background noise).", color="green")

def test_injection(iface_out, iface_in=None, peermac=None):
	# We start monitoring iface_in already so injected frame won't be missed
	sout = L2Socket(type=ETH_P_ALL, iface=iface_out)
	driver_out = get_device_driver(iface_out)
	# Use the Intel workaround if needed
	sout.intel_mf_workaround = driver_out == "iwlwifi"

	log(STATUS, f"Injection test: using {iface_out} ({driver_out}) to inject frames")
	if iface_in == None:
		log(WARNING, f"Injection selftest: also using {iface_out} to capture frames. This means the tests can detect if the kernel")
		log(WARNING, f"                    interferes with injection, but it cannot check the behaviour of the device itself.")
		sin = sout
	else:
		driver_in = get_device_driver(iface_in)
		log(STATUS, f"Injection test: using {iface_in} ({driver_in}) to capture frames")
		sin = L2Socket(type=ETH_P_ALL, iface=iface_in)

	# Get own MAC address for tests and construct reference headers
	ownmac = get_macaddress(sout.iface)
	spoofed = Dot11(addr1="00:11:00:00:02:01", addr2="00:22:00:00:02:01")
	valid = Dot11(addr1=peermac, addr2=ownmac)

	# This tests basic injection capabilities
	test_injection_fragment(sout, sin, valid)

	# Perform some actual injection tests
	test_injection_fields(sout, sin, spoofed, "spoofed MAC addresses")
	test_injection_fields(sout, sin, valid, "(partly) valid MAC addresses")
	test_injection_order(sout, sin, spoofed, "spoofed MAC addresses")
	test_injection_order(sout, sin, valid, "(partly) valid MAC addresses")

	# Acknowledgement behaviour tests
	if iface_in != None:
		# We search for an AP on the interface that injects frames because:
		# 1. In mixed managed/monitor mode, we will otherwise detect our own AP on the sout interface
		# 2. If sout interface "sees" the AP this assure it will also receive its ACK frames
		# 3. The given peermac might be a client that goes into sleep mode
		channel = get_channel(sin.iface)
		log(STATUS, f"Searching for AP on channel {channel} to test ACK behaviour.")
		apmac, ssid = get_nearby_ap_addr(sout)
		if apmac == None and peermac == None:
			raise IOError("Unable to find nearby AP to test injection")
		elif apmac ==None:
			log(STATUS, f"Unable to find AP. Testing ACK behaviour with peer {peermac}.")
		else:
			log(STATUS, f"Testing ACK behaviour by injecting frames to AP {ssid} ({apmac}).")
		test_injection_ack(sout, sin, addr1=apmac, addr2=ownmac)

	sout.close()
	sin.close()

#### Packet Processing Functions ####

class DHCP_sock(DHCP_am):
	def __init__(self, **kwargs):
		self.sock = kwargs.pop("sock")
		self.server_ip = kwargs["gw"]
		super(DHCP_sock, self).__init__(**kwargs)

	def prealloc_ip(self, clientmac, ip=None):
		"""Allocate an IP for the client before it send DHCP requests"""
		if clientmac not in self.leases:
			if ip == None:
				ip = self.pool.pop()
			self.leases[clientmac] = ip
		return self.leases[clientmac]

	def make_reply(self, req):
		rep = super(DHCP_sock, self).make_reply(req)

		# Fix scapy bug: set broadcast IP if required
		if rep is not None and BOOTP in req and IP in rep:
			if req[BOOTP].flags & 0x8000 != 0 and req[BOOTP].giaddr == '0.0.0.0' and req[BOOTP].ciaddr == '0.0.0.0':
				rep[IP].dst = "255.255.255.255"

		# Explicitly set source IP if requested
		if not self.server_ip is None:
			rep[IP].src = self.server_ip

		return rep

	def send_reply(self, reply):
		self.sock.send(reply, **self.optsend)

	def print_reply(self, req, reply):
		log(STATUS, "%s: DHCP reply %s to %s" % (reply.getlayer(Ether).dst, reply.getlayer(BOOTP).yiaddr, reply.dst), color="green")

	def remove_client(self, clientmac):
		clientip = self.leases[clientmac]
		self.pool.append(clientip)
		del self.leases[clientmac]

class ARP_sock(ARP_am):
	def __init__(self, **kwargs):
		self.sock = kwargs.pop("sock")
		super(ARP_am, self).__init__(**kwargs)

	def send_reply(self, reply):
		self.sock.send(reply, **self.optsend)

	def print_reply(self, req, reply):
		log(STATUS, "%s: ARP: %s ==> %s on %s" % (reply.getlayer(Ether).dst, req.summary(), reply.summary(), self.iff))


#### Packet Processing Functions ####

class MonitorSocket(L2Socket):
	def __init__(self, detect_injected=False, **kwargs):
		super(MonitorSocket, self).__init__(**kwargs)
		self.detect_injected = detect_injected

	def send(self, p):
		# Hack: set the More Data flag so we can detect injected frames (and so clients stay awake longer)
		if self.detect_injected:
			p.FCfield |= 0x20
		L2Socket.send(self, RadioTap()/p)

	def _strip_fcs(self, p):
		# Older scapy can't handle the optional Frame Check Sequence (FCS) field automatically
		if p[RadioTap].present & 2 != 0 and not Dot11FCS in p:
			rawframe = raw(p[RadioTap])
			pos = 8
			while orb(rawframe[pos - 1]) & 0x80 != 0: pos += 4

			# If the TSFT field is present, it must be 8-bytes aligned
			if p[RadioTap].present & 1 != 0:
				pos += (8 - (pos % 8))
				pos += 8

			# Remove FCS if present
			if orb(rawframe[pos]) & 0x10 != 0:
				return Dot11(raw(p[Dot11])[:-4])

		return p[Dot11]

	def recv(self, x=MTU, reflected=False):
		p = L2Socket.recv(self, x)
		if p == None or not (Dot11 in p or Dot11FCS in p):
			return None

		# Hack: ignore frames that we just injected and are echoed back by the kernel
		if self.detect_injected and p.FCfield & 0x20 != 0:
			return None

		# Ignore reflection of injected frames. These have a small RadioTap header.
		if not reflected and p[RadioTap].len <= 13:
			return None

		# Strip the FCS if present, and drop the RadioTap header
		if Dot11FCS in p:
			return Dot11(raw(p[Dot11FCS])[:-4])
		else:
			return self._strip_fcs(p)

	def close(self):
		super(MonitorSocket, self).close()

# For backwards compatibility
class MitmSocket(MonitorSocket):
	pass

def dot11_get_seqnum(p):
	return p.SC >> 4

def dot11_is_encrypted_data(p):
	# All these different cases are explicitly tested to handle older scapy versions
	return (p.FCfield & 0x40) or Dot11CCMP in p or Dot11TKIP in p or Dot11WEP in p or Dot11Encrypted in p

def payload_to_iv(payload):
	iv0 = payload[0]
	iv1 = payload[1]
	wepdata = payload[4:8]

	# FIXME: Only CCMP is supported (TKIP uses a different IV structure)
	return ord(iv0) + (ord(iv1) << 8) + (struct.unpack(">I", wepdata)[0] << 16)

def dot11_get_iv(p):
	"""
	Assume it's a CCMP frame. Old scapy can't handle Extended IVs.
	This code only works for CCMP frames.
	"""
	if Dot11CCMP in p or Dot11TKIP in p or Dot11Encrypted in p:
		# Scapy uses a heuristic to differentiate CCMP/TKIP and this may be wrong.
		# So even when we get a Dot11TKIP frame, we should treat it like a Dot11CCMP frame.
		payload = str(p[Dot11Encrypted])
		return payload_to_iv(payload)

	elif Dot11WEP in p:
		wep = p[Dot11WEP]
		if wep.keyid & 32:
			# FIXME: Only CCMP is supported (TKIP uses a different IV structure)
			return ord(wep.iv[0]) + (ord(wep.iv[1]) << 8) + (struct.unpack(">I", wep.wepdata[:4])[0] << 16)
		else:
			return ord(wep.iv[0]) + (ord(wep.iv[1]) << 8) + (ord(wep.iv[2]) << 16)

	elif p.FCfield & 0x40:
		return payload_to_iv(p[Raw].load)

	else:
		log(ERROR, "INTERNAL ERROR: Requested IV of plaintext frame")
		return 0

def get_tlv_value(p, type):
	if not Dot11Elt in p: return None
	el = p[Dot11Elt]
	while isinstance(el, Dot11Elt):
		if el.ID == type:
			return el.info
		el = el.payload
	return None

def dot11_get_priority(p):
	if not Dot11QoS in p: return 0
	return p[Dot11QoS].TID


#### Crypto functions and util ####

def get_ccmp_payload(p):
	if Dot11WEP in p:
		# Extract encrypted payload:
		# - Skip extended IV (4 bytes in total)
		# - Exclude first 4 bytes of the CCMP MIC (note that last 4 are saved in the WEP ICV field)
		return str(p.wepdata[4:-4])
	elif Dot11CCMP in p or Dot11TKIP in p or Dot11Encrypted in p:
		return p[Dot11Encrypted].data
	else:
		return p[Raw].load

class IvInfo():
	def __init__(self, p):
		self.iv = dot11_get_iv(p)
		self.seq = dot11_get_seqnum(p)
		self.time = p.time

	def is_reused(self, p):
		"""Return true if frame p reuses an IV and if p is not a retransmitted frame"""
		iv = dot11_get_iv(p)
		seq = dot11_get_seqnum(p)
		return self.iv == iv and self.seq != seq and p.time >= self.time + 1

class IvCollection():
	def __init__(self):
		self.ivs = dict() # maps IV values to IvInfo objects

	def reset(self):
		self.ivs = dict()

	def track_used_iv(self, p):
		iv = dot11_get_iv(p)
		self.ivs[iv] = IvInfo(p)

	def is_iv_reused(self, p):
		"""Returns True if this is an *observed* IV reuse and not just a retransmission"""
		iv = dot11_get_iv(p)
		return iv in self.ivs and self.ivs[iv].is_reused(p)

	def is_new_iv(self, p):
		"""Returns True if the IV in this frame is higher than all previously observed ones"""
		iv = dot11_get_iv(p)
		if len(self.ivs) == 0: return True
		return iv > max(self.ivs.keys())

def create_fragments(header, data, num_frags):
	data = raw(data)
	fragments = []
	fragsize = (len(data) + num_frags - 1) // num_frags
	for i in range(num_frags):
		frag = header.copy()
		frag.SC |= i
		if i < num_frags - 1:
			frag.FCfield |= Dot11(FCfield="MF").FCfield

		payload = data[fragsize * i : fragsize * (i + 1)]
		frag = frag/Raw(payload)
		fragments.append(frag)

	return fragments

def get_element(el, id):
	el = el[Dot11Elt]
	while not el is None:
		if el.ID == id:
			return el
		el = el.payload
	return None

def get_ssid(beacon):
	if not (Dot11 in beacon or Dot11FCS in beacon): return
	if Dot11Elt not in beacon: return
	if beacon[Dot11].type != 0 and beacon[Dot11].subtype != 8: return
	el = get_element(beacon, 0)
	return el.info.decode()

