package relay

import (
	"bytes"
	"testing"
	"time"

	"github.com/pion/rtp"
)

func packet(seq uint16, ts uint32, marker bool, payload ...byte) *rtp.Packet {
	return &rtp.Packet{Header: rtp.Header{SequenceNumber: seq, Timestamp: ts, Marker: marker}, Payload: payload}
}

func annexB(nalus ...[]byte) []byte {
	var out []byte
	for _, nalu := range nalus {
		out = append(out, 0, 0, 0, 1)
		out = append(out, nalu...)
	}
	return out
}

func collect() (*Assembler, *[]Frame) {
	frames := &[]Frame{}
	return NewAssembler(func(f Frame) { *frames = append(*frames, f) }), frames
}

func TestFramesSplitOnMarkerAndTimestamp(t *testing.T) {
	a, frames := collect()
	a.Push(packet(1, 1000, false, 0x67, 0xAA)) // SPS
	a.Push(packet(2, 1000, false, 0x68, 0xBB)) // PPS
	a.Push(packet(3, 1000, true, 0x65, 0x01))  // IDR, marker
	a.Push(packet(4, 7000, false, 0x41, 0x02)) // P frame, no marker...
	a.Push(packet(5, 13000, true, 0x41, 0x03)) // ...ends when the timestamp moves on

	if len(*frames) != 3 {
		t.Fatalf("got %d frames, want 3", len(*frames))
	}
	first := (*frames)[0]
	if !first.Keyframe || !bytes.Equal(first.Data, annexB([]byte{0x67, 0xAA}, []byte{0x68, 0xBB}, []byte{0x65, 0x01})) {
		t.Fatalf("keyframe = %+v", first)
	}
	if (*frames)[1].Keyframe || !bytes.Equal((*frames)[1].Data, annexB([]byte{0x41, 0x02})) {
		t.Fatalf("P frame = %+v", (*frames)[1])
	}
	// 6000 ticks of the 90 kHz clock.
	if got := (*frames)[2].Duration; got != 6000*time.Second/90000 {
		t.Fatalf("duration = %v", got)
	}
}

func TestFragmentedFramesAreReassembled(t *testing.T) {
	a, frames := collect()
	// FU-A: indicator 0x7C (NRI 3, type 28); headers start 0x85 / end 0x45 for IDR.
	a.Push(packet(10, 500, false, 0x7C, 0x85, 0x01, 0x02))
	a.Push(packet(11, 500, false, 0x7C, 0x05, 0x03))
	a.Push(packet(12, 500, true, 0x7C, 0x45, 0x04))
	if len(*frames) != 1 || !bytes.Equal((*frames)[0].Data, annexB([]byte{0x65, 0x01, 0x02, 0x03, 0x04})) {
		t.Fatalf("frames = %+v", *frames)
	}
}

func TestKeyframeWithoutParameterSetsGetsCachedOnes(t *testing.T) {
	a, frames := collect()
	a.Push(packet(1, 0, false, 0x67, 0xAA))
	a.Push(packet(2, 0, false, 0x68, 0xBB))
	a.Push(packet(3, 0, true, 0x65, 0x01))
	a.Push(packet(4, 3000, true, 0x65, 0x02)) // later IDR sent without SPS/PPS
	got := (*frames)[1]
	want := annexB([]byte{0x67, 0xAA}, []byte{0x68, 0xBB}, []byte{0x65, 0x02})
	if !got.Keyframe || !bytes.Equal(got.Data, want) {
		t.Fatalf("got %x, want %x", got.Data, want)
	}
}

func TestPacketBuffersAreNotRetained(t *testing.T) {
	a, frames := collect()
	payload := []byte{0x41, 0x09}
	a.Push(packet(1, 0, false, payload...))
	payload[1] = 0xFF // the caller reuses its buffer
	a.Push(packet(2, 0, true))
	if !bytes.Equal((*frames)[0].Data, annexB([]byte{0x41, 0x09})) {
		t.Fatalf("frame aliased the packet buffer: %x", (*frames)[0].Data)
	}
}
