package protocol

import (
	"encoding/binary"
	"fmt"
)

const (
	recordVersion = 0xf103

	ctAlert        byte = 0x15
	ctHandshake    byte = 0x16
	ctAppData      byte = 0x17
	ctPSKHandshake byte = 0x19

	hsClientHello      byte = 0x01
	hsServerHello      byte = 0x02
	hsNewSessionTicket byte = 0x04
	hsCertVerify       byte = 0x0f
	hsFinished         byte = 0x14

	cipherECDHE = 0xc02b
	cipherPSK   = 0x00a8

	extKeyShare    byte   = 0x01
	groupSecP256R1 uint32 = 0x01
	group2         uint32 = 0x02
	keyLen                = 65

	shortlinkMagic = 0x1110
	defaultVer     = 0x076d
	cmdManualAuth  = 0x0d7d
)

type record struct {
	ContentType byte
	Payload     []byte
}

func buildRecord(contentType byte, payload []byte) ([]byte, error) {
	if len(payload) > 0xffff {
		return nil, fmt.Errorf("record payload too long: %d", len(payload))
	}
	out := make([]byte, 5+len(payload))
	out[0] = contentType
	binary.BigEndian.PutUint16(out[1:3], recordVersion)
	binary.BigEndian.PutUint16(out[3:5], uint16(len(payload)))
	copy(out[5:], payload)
	return out, nil
}

func parseRecords(data []byte) ([]record, int) {
	var recs []record
	off := 0
	for off+5 <= len(data) {
		ct := data[off]
		ver := binary.BigEndian.Uint16(data[off+1 : off+3])
		ln := int(binary.BigEndian.Uint16(data[off+3 : off+5]))
		if ver != recordVersion || !knownContentType(ct) {
			break
		}
		end := off + 5 + ln
		if end > len(data) {
			break
		}
		recs = append(recs, record{ContentType: ct, Payload: append([]byte(nil), data[off+5:end]...)})
		off = end
	}
	return recs, off
}

func knownContentType(ct byte) bool {
	return ct == ctAlert || ct == ctHandshake || ct == ctAppData || ct == ctPSKHandshake || ct == 0x14
}

func buildHandshake(msgType byte, body []byte) []byte {
	total := 1 + len(body)
	out := make([]byte, 4+total)
	binary.BigEndian.PutUint32(out[0:4], uint32(total))
	out[4] = msgType
	copy(out[5:], body)
	return out
}

func splitHandshake(payload []byte) (byte, []byte, error) {
	if len(payload) < 5 {
		return 0, nil, fmt.Errorf("handshake payload too short")
	}
	total := int(binary.BigEndian.Uint32(payload[:4]))
	if total <= 0 || 4+total > len(payload) {
		return 0, nil, fmt.Errorf("invalid handshake length")
	}
	inner := payload[4 : 4+total]
	return inner[0], inner[1:], nil
}

func buildClientHello(clientRandom []byte, timestamp uint32, g1Pub, g2Pub []byte) ([]byte, error) {
	if len(clientRandom) != 32 || len(g1Pub) != keyLen || len(g2Pub) != keyLen {
		return nil, fmt.Errorf("invalid ClientHello key/random length")
	}
	body := make([]byte, 0, 230)
	body = append(body, 0x03, 0xf1)
	body = append(body, 1)
	var tmp [4]byte
	binary.BigEndian.PutUint16(tmp[:2], cipherECDHE)
	body = append(body, tmp[:2]...)
	body = append(body, clientRandom...)
	binary.BigEndian.PutUint32(tmp[:4], timestamp)
	body = append(body, tmp[:4]...)

	ks := make([]byte, 0, 180)
	ks = append(ks, 0x00, 0x10, 2)
	for _, offer := range []struct {
		group uint32
		pub   []byte
	}{{groupSecP256R1, g1Pub}, {group2, g2Pub}} {
		one := make([]byte, 0, 4+2+65)
		binary.BigEndian.PutUint32(tmp[:4], offer.group)
		one = append(one, tmp[:4]...)
		binary.BigEndian.PutUint16(tmp[:2], keyLen)
		one = append(one, tmp[:2]...)
		one = append(one, offer.pub...)
		binary.BigEndian.PutUint32(tmp[:4], uint32(len(one)))
		ks = append(ks, tmp[:4]...)
		ks = append(ks, one...)
	}
	ks = append(ks, 0, 0, 0, 1)
	ext := []byte{extKeyShare}
	binary.BigEndian.PutUint32(tmp[:4], uint32(len(ks)))
	ext = append(ext, tmp[:4]...)
	ext = append(ext, ks...)
	binary.BigEndian.PutUint32(tmp[:4], uint32(len(ext)))
	body = append(body, tmp[:4]...)
	body = append(body, ext...)
	return buildHandshake(hsClientHello, body), nil
}

func buildPSKClientHello(clientRandom []byte, timestamp uint32, ticketEntry []byte) ([]byte, error) {
	if len(clientRandom) != 32 {
		return nil, fmt.Errorf("client random must be 32 bytes")
	}
	body := make([]byte, 0, 60+len(ticketEntry))
	body = append(body, 0x03, 0xf1, 1)
	var tmp [4]byte
	binary.BigEndian.PutUint16(tmp[:2], cipherPSK)
	body = append(body, tmp[:2]...)
	body = append(body, clientRandom...)
	binary.BigEndian.PutUint32(tmp[:4], timestamp)
	body = append(body, tmp[:4]...)
	extData := []byte{0x00, 0x0f, 1}
	binary.BigEndian.PutUint32(tmp[:4], uint32(len(ticketEntry)))
	extData = append(extData, tmp[:4]...)
	extData = append(extData, ticketEntry...)
	ext := []byte{extKeyShare}
	binary.BigEndian.PutUint32(tmp[:4], uint32(len(extData)))
	ext = append(ext, tmp[:4]...)
	ext = append(ext, extData...)
	binary.BigEndian.PutUint32(tmp[:4], uint32(len(ext)))
	body = append(body, tmp[:4]...)
	body = append(body, ext...)
	return buildHandshake(hsClientHello, body), nil
}

type serverHello struct {
	ServerRandom []byte
	Cipher       uint16
	ServerGroup  uint32
	ServerPubKey []byte
}

func parseServerHello(body []byte) (serverHello, error) {
	if len(body) < 2+2+32+4 {
		return serverHello{}, fmt.Errorf("ServerHello too short")
	}
	o := 0
	o += 2
	cipher := binary.BigEndian.Uint16(body[o : o+2])
	o += 2
	random := append([]byte(nil), body[o:o+32]...)
	o += 32
	extLen := int(binary.BigEndian.Uint32(body[o : o+4]))
	o += 4
	if o+extLen > len(body) {
		return serverHello{}, fmt.Errorf("ServerHello extension truncated")
	}
	ext := body[o : o+extLen]
	out := serverHello{ServerRandom: random, Cipher: cipher, ServerGroup: groupSecP256R1}
	if len(ext) > 0 && ext[0] == extKeyShare {
		eo := 1
		if eo+4 > len(ext) {
			return out, fmt.Errorf("ServerKeyShare truncated")
		}
		_ = binary.BigEndian.Uint32(ext[eo : eo+4])
		eo += 4
		eo += 2
		if eo+4+2 > len(ext) {
			return out, fmt.Errorf("ServerKeyShare truncated")
		}
		out.ServerGroup = binary.BigEndian.Uint32(ext[eo : eo+4])
		eo += 4
		klen := int(binary.BigEndian.Uint16(ext[eo : eo+2]))
		eo += 2
		if eo+klen > len(ext) {
			return out, fmt.Errorf("ServerKeyShare key truncated")
		}
		out.ServerPubKey = append([]byte(nil), ext[eo:eo+klen]...)
		return out, nil
	}
	keys := extractKeyShares(ext)
	if len(keys) > 0 {
		out.ServerPubKey = keys[0]
	}
	return out, nil
}

func extractKeyShares(blob []byte) [][]byte {
	var keys [][]byte
	for i := 0; i+67 <= len(blob); {
		if blob[i] == 0x00 && blob[i+1] == 0x41 && blob[i+2] == 0x04 {
			keys = append(keys, append([]byte(nil), blob[i+2:i+2+65]...))
			i += 67
		} else {
			i++
		}
	}
	return keys
}

func buildFinished(verifyData []byte) ([]byte, error) {
	if len(verifyData) != 32 {
		return nil, fmt.Errorf("verify_data must be 32 bytes")
	}
	body := make([]byte, 2+len(verifyData))
	binary.BigEndian.PutUint16(body[:2], uint16(len(verifyData)))
	copy(body[2:], verifyData)
	return buildHandshake(hsFinished, body), nil
}

func parseFinished(body []byte) ([]byte, error) {
	if len(body) < 2 {
		return nil, fmt.Errorf("Finished too short")
	}
	n := int(binary.BigEndian.Uint16(body[:2]))
	if 2+n > len(body) {
		return nil, fmt.Errorf("Finished truncated")
	}
	return append([]byte(nil), body[2:2+n]...), nil
}

type pskTicket struct {
	PSKType     int
	Lifetime    int64
	TicketEntry []byte
}

func parseNewSessionTicket(body []byte) []pskTicket {
	if len(body) == 0 {
		return nil
	}
	count := int(body[0])
	o := 1
	out := make([]pskTicket, 0, count)
	for i := 0; i < count && o+4 <= len(body); i++ {
		elen := int(binary.BigEndian.Uint32(body[o : o+4]))
		o += 4
		if o+elen > len(body) || elen < 5 {
			break
		}
		entry := append([]byte(nil), body[o:o+elen]...)
		o += elen
		out = append(out, pskTicket{
			PSKType:     int(entry[0]),
			Lifetime:    int64(binary.BigEndian.Uint32(entry[1:5])),
			TicketEntry: entry,
		})
	}
	return out
}

func buildShortlink(cmd uint32, seq uint32, body []byte, ver uint16) []byte {
	if ver == 0 {
		ver = defaultVer
	}
	total := 16 + len(body)
	out := make([]byte, total)
	binary.BigEndian.PutUint32(out[0:4], uint32(total))
	binary.BigEndian.PutUint16(out[4:6], shortlinkMagic)
	binary.BigEndian.PutUint16(out[6:8], ver)
	binary.BigEndian.PutUint32(out[8:12], cmd)
	binary.BigEndian.PutUint32(out[12:16], seq)
	copy(out[16:], body)
	return out
}

type shortlinkPacket struct {
	TotalLen int
	Magic    uint16
	Ver      uint16
	Cmd      uint32
	Seq      uint32
	Body     []byte
}

func parseShortlink(pkt []byte) (shortlinkPacket, error) {
	if len(pkt) < 16 {
		return shortlinkPacket{}, fmt.Errorf("shortlink packet too short")
	}
	total := int(binary.BigEndian.Uint32(pkt[:4]))
	if total < 16 || total > len(pkt) {
		return shortlinkPacket{}, fmt.Errorf("shortlink packet truncated")
	}
	return shortlinkPacket{
		TotalLen: total,
		Magic:    binary.BigEndian.Uint16(pkt[4:6]),
		Ver:      binary.BigEndian.Uint16(pkt[6:8]),
		Cmd:      binary.BigEndian.Uint32(pkt[8:12]),
		Seq:      binary.BigEndian.Uint32(pkt[12:16]),
		Body:     append([]byte(nil), pkt[16:total]...),
	}, nil
}
