#!/usr/bin/python3
import sys
import struct
import wrapper
import threading
import time
from enum import Enum
from wrapper import recv_from_any_link, send_to_link, get_switch_mac, get_interface_name

trunk_interfaces = []
isRoot = True
bridge_id = 0
root_id = 0
root_path_cost = 0

class State(Enum):
    ROOT = 0
    DESIGNATED = 1
    BLOCKED = 2

def parse_ethernet_header(data):
    # Unpack the header fields from the byte array
    #dest_mac, src_mac, ethertype = struct.unpack('!6s6sH', data[:14])
    dest_mac = data[0:6]
    src_mac = data[6:12]

    # Extract ethertype. Under 802.1Q, this may be the bytes from the VLAN TAG
    ether_type = (data[12] << 8) + data[13]

    vlan_id = -1
    # Check for VLAN tag (0x8100 in network byte order is b'\x81\x00')
    if ether_type == 0x8200:
        vlan_tci = int.from_bytes(data[14:16], byteorder='big')
        vlan_id = vlan_tci & 0x0FFF  # extract the 12-bit VLAN ID
        ether_type = (data[16] << 8) + data[17]

    return dest_mac, src_mac, ether_type, vlan_id

def create_vlan_tag(vlan_id):
    # 0x8100 for the Ethertype for 802.1Q
    # vlan_id & 0x0FFF ensures that only the last 12 bits are used
    return struct.pack('!H', 0x8200) + struct.pack('!H', vlan_id & 0x0FFF)

def create_bdpu(root_id, root_path_cost, bridge_id):

    llc_length = struct.pack('!H', 52)
    llc_header = struct.pack('!B', 0x42) + struct.pack('!B', 0x42) + struct.pack('!B', 0x03) 
    bdpu_header = struct.pack('!L', 0)
    bpdu_config = struct.pack('!B', 0) + struct.pack('!Q', root_id) + struct.pack('!L', root_path_cost) + \
                  struct.pack('!Q', bridge_id) + struct.pack('!H', 0) + \
                  struct.pack('!H', 0) + struct.pack('!H', 0) + \
                  struct.pack('!H', 0) + struct.pack('!H', 0)

    bpdu_frame = bytes([int(x, 16) for x in "01:80:c2:00:00:00".split(':')]) + get_switch_mac() +  llc_length +  llc_header + bdpu_header + bpdu_config
    return bpdu_frame


def send_bdpu_every_sec():
    global trunk_interfaces, root_id, bridge_id, root_path_cost, isRoot
    while True:
        if isRoot == True: 
            for i in trunk_interfaces:
                send_to_link(i, create_bdpu(root_id=root_id, root_path_cost=0, bridge_id=root_id), 52)
        time.sleep(1)

def main():
    # init returns the max interface number. Our interfaces
    # are 0, 1, 2, ..., init_ret value + 1
    global isRoot, trunk_interfaces, bridge_id, root_id, root_path_cost

    switch_id = sys.argv[1]
    num_interfaces = wrapper.init(sys.argv[2:])
    interfaces = range(0, num_interfaces)

    with open("configs/switch{}.cfg".format(switch_id), "r") as f:
        priority = int(f.readline().strip())
        vlan_table = {}
        for line in f:
            interface, vlan_id = line.strip().split()
            vlan_table.update({interface: vlan_id})

    
    trunk_interfaces = [i for i in interfaces if vlan_table[get_interface_name(i)] == "T"]
    state_interfaces = {i: State.BLOCKED for i in trunk_interfaces}
    bridge_id = priority
    root_id = bridge_id
    root_path_cost = 0
    root_port = -1
    isRoot = True

    state_interfaces = {i: State.DESIGNATED for i in trunk_interfaces if isRoot == True} 

    cam_table = {}

    print("# Starting switch with id {}".format(switch_id), flush=True)
    print("[INFO] Switch MAC", ':'.join(f'{b:02x}' for b in get_switch_mac()))
    print("[INFO] Priority", priority)
    print("[INFO] VLAN Table", vlan_table)

    # Create and start a new thread that deals with sending BDPU
    t = threading.Thread(target=send_bdpu_every_sec)
    t.start()

    # Printing interface names
    for i in interfaces:
        print(get_interface_name(i))

    while True:
        # Note that data is of type bytes([...]).
        # b1 = bytes([72, 101, 108, 108, 111])  # "Hello"
        # b2 = bytes([32, 87, 111, 114, 108, 100])  # " World"
        # b3 = b1[0:2] + b[3:4].
        interface, data, length = recv_from_any_link()


        dest_mac, src_mac, ethertype, vlan_id = parse_ethernet_header(data)

        # Print the MAC src and MAC dst in human readable format
        dest_mac = ':'.join(f'{b:02x}' for b in dest_mac)
        src_mac = ':'.join(f'{b:02x}' for b in src_mac)


        # Note. Adding a VLAN tag can be as easy as
        # tagged_frame = data[0:12] + create_vlan_tag(10) + data[12:]
        # remove vlan tag from data 
        if dest_mac != "01:80:c2:00:00:00":

            vid_in = vlan_table[get_interface_name(interface)]
            cam_table.update({src_mac: interface})
            if dest_mac != "ff:ff:ff:ff:ff:ff":
                if dest_mac in cam_table:
                    out_interface = cam_table[dest_mac]
                    vid_out = vlan_table[get_interface_name(out_interface)]
                    if vid_in =="T":
                        if vid_out == "T" and state_interfaces[out_interface] != State.BLOCKED:
                            send_to_link(out_interface, data, length)
                        elif vlan_id == int(vid_out):
                            send_to_link(out_interface, data[0:12] + data[16:], length - 4)

                    else:
                        if vid_out == "T" and state_interfaces[out_interface] != State.BLOCKED:
                            tagged_frame = data[0:12] + create_vlan_tag(int(vid_in)) + data[12:]
                            send_to_link(out_interface, tagged_frame, length + 4)
                        elif int(vid_in) == int(vid_out):
                            send_to_link(out_interface, data, length)

                else:
                    for i in interfaces:
                        if i != interface:
                            vid_out = vlan_table[get_interface_name(i)]
                            if vid_in =="T":
                                if vid_out == "T" and state_interfaces[i] != State.BLOCKED:
                                    send_to_link(i, data, length)
                                elif vid_out != "T" and vlan_id == int(vid_out):
                                    send_to_link(i, data[0:12] + data[16:], length - 4)

                            else:
                                if vid_out == "T" and state_interfaces[i] != State.BLOCKED:
                                    tagged_frame = data[0:12] + create_vlan_tag(int(vid_in)) + data[12:]
                                    send_to_link(i, tagged_frame, length + 4)
                                elif vid_out != "T" and int(vid_in) == int(vid_out):
                                    send_to_link(i, data, length)

            else:
                for i in interfaces:
                    if i != interface:
                        vid_out = vlan_table[get_interface_name(i)]
                        if vid_in =="T":
                            if vid_out == "T" and state_interfaces[i] != State.BLOCKED:
                                send_to_link(i, data, length)
                            elif vid_out != "T" and vlan_id == int(vid_out):
                                send_to_link(i, data[0:12] + data[16:], length - 4)
                        else :
                            if vid_out == "T" and state_interfaces[i] != State.BLOCKED:
                                tagged_frame = data[0:12] + create_vlan_tag(int(vid_in)) + data[12:]
                                send_to_link(i, tagged_frame, length + 4)
                            elif vid_out != "T" and int(vid_in) == int(vid_out):
                                send_to_link(i, data, length)

        else:
            bdpu_root_id = int.from_bytes(data[22:30], byteorder='big')
            bdpu_root_path_cost = int.from_bytes(data[30:34], byteorder='big')
            bdpu_bridge_id = int.from_bytes(data[34:42], byteorder='big')
            isRoot = (root_id == bridge_id)

            if bdpu_root_id < root_id:
                root_id = bdpu_root_id
                root_path_cost = bdpu_root_path_cost + 10
                root_port = interface

                if isRoot == True:
                    for i in trunk_interfaces:
                        if i != root_port:
                            state_interfaces.update({i: State.BLOCKED})

                if state_interfaces[root_port] == State.BLOCKED:
                    state_interfaces.update({root_port: State.DESIGNATED})

                for i in trunk_interfaces:
                    if i != root_port:
                        send_to_link(i, data[0:30]+ struct.pack('!L',root_path_cost) + struct.pack('!Q',bridge_id) + data[42:], 52)

            elif bdpu_root_id == root_id:
                if interface == root_port and bdpu_root_path_cost + 10 < root_path_cost:
                    root_path_cost = bdpu_root_path_cost + 10

                elif interface != root_port:
                    if bdpu_root_path_cost > root_path_cost:
                        if state_interfaces[interface] != State.DESIGNATED:
                            state_interfaces.update({interface: State.DESIGNATED})

            elif bdpu_bridge_id == bridge_id:
                state_interfaces.update({interface: State.BLOCKED})

            else:
                continue

            if bridge_id == root_id:
                for i in trunk_interfaces:
                    state_interfaces.update({i: State.DESIGNATED})

        # data is of type bytes.
        # send_to_link(i, data, length)

if __name__ == "__main__":
    main()
