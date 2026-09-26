package relay

import (
	"time"

	"github.com/pion/rtp"

	"vico_home/native/internal/webrtc"
)

const (
	naluIDR = 5
	naluSPS = 7
	naluPPS = 8

	defaultFrameDuration = 66 * time.Millisecond
)

var startCode = []byte{0, 0, 0, 1}

// Frame is one H264 access unit in Annex-B form.
type Frame struct {
	Data     []byte
	Duration time.Duration
	Keyframe bool
}

// Assembler turns camera RTP packets into whole frames that a local track can
// re-packetize for browsers. The camera's own packetization (which it labels
// packetization-mode=0) never reaches a browser.
type Assembler struct {
	depack   *webrtc.H264Depacketizer
	emit     func(Frame)
	nalus    [][]byte
	ts       uint32
	pending  bool
	lastTS   uint32
	haveLast bool
	sps, pps []byte
}

func NewAssembler(emit func(Frame)) *Assembler {
	return &Assembler{depack: webrtc.NewH264Depacketizer(), emit: emit}
}

// Push adds one RTP packet. A frame is emitted on its marker bit, or when a
// packet with a new timestamp shows the previous frame ended without one.
func (a *Assembler) Push(pkt *rtp.Packet) {
	if a.pending && pkt.Timestamp != a.ts {
		a.flush()
	}
	a.ts, a.pending = pkt.Timestamp, true
	for _, nalu := range a.depack.Depacketize(pkt.SequenceNumber, pkt.Payload) {
		if len(nalu) == 0 {
			continue
		}
		owned := append([]byte(nil), nalu...)
		switch owned[0] & 0x1f {
		case naluSPS:
			a.sps = owned
		case naluPPS:
			a.pps = owned
		}
		a.nalus = append(a.nalus, owned)
	}
	if pkt.Marker {
		a.flush()
	}
}

func (a *Assembler) flush() {
	nalus := a.nalus
	a.nalus, a.pending = nil, false
	if len(nalus) == 0 {
		return
	}
	keyframe, hasSPS, hasPPS := false, false, false
	for _, nalu := range nalus {
		switch nalu[0] & 0x1f {
		case naluIDR:
			keyframe = true
		case naluSPS:
			hasSPS = true
		case naluPPS:
			hasPPS = true
		}
	}
	// A browser that joins mid-stream can only start decoding at a keyframe
	// that carries its parameter sets.
	if keyframe {
		var prefix [][]byte
		if !hasSPS && a.sps != nil {
			prefix = append(prefix, a.sps)
		}
		if !hasPPS && a.pps != nil {
			prefix = append(prefix, a.pps)
		}
		nalus = append(prefix, nalus...)
	}
	size := 0
	for _, nalu := range nalus {
		size += len(startCode) + len(nalu)
	}
	data := make([]byte, 0, size)
	for _, nalu := range nalus {
		data = append(data, startCode...)
		data = append(data, nalu...)
	}
	duration := defaultFrameDuration
	if a.haveLast {
		// 90 kHz RTP clock; wraparound is handled by unsigned subtraction.
		if delta := time.Duration(a.ts-a.lastTS) * time.Second / 90000; delta > 0 && delta <= time.Second {
			duration = delta
		}
	}
	a.lastTS, a.haveLast = a.ts, true
	a.emit(Frame{Data: data, Duration: duration, Keyframe: keyframe})
}
