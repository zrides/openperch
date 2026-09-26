package relay

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	pion "github.com/pion/webrtc/v4"
)

type lockedBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *lockedBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *lockedBuffer) events() []map[string]any {
	b.mu.Lock()
	defer b.mu.Unlock()
	var out []map[string]any
	scanner := bufio.NewScanner(strings.NewReader(b.buf.String()))
	for scanner.Scan() {
		var event map[string]any
		if json.Unmarshal(scanner.Bytes(), &event) == nil {
			out = append(out, event)
		}
	}
	return out
}

func newTestRelay(t *testing.T, opts Options) (*Relay, *lockedBuffer, context.Context) {
	t.Helper()
	ctx, cancel := context.WithCancelCause(context.Background())
	t.Cleanup(func() { cancel(nil) })
	events := &lockedBuffer{}
	if opts.IdleTimeout == 0 {
		opts.IdleTimeout = time.Minute
	}
	if opts.StartTimeout == 0 {
		opts.StartTimeout = time.Minute
	}
	r, err := New(opts, "1280x720", events, cancel)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(r.Close)
	return r, events, ctx
}

// A browser-like viewer: recvonly H264, non-trickle offer posted to /whep.
func connectViewer(t *testing.T, url string) (*pion.PeerConnection, <-chan *pion.TrackRemote) {
	t.Helper()
	engine := &pion.MediaEngine{}
	if err := engine.RegisterDefaultCodecs(); err != nil {
		t.Fatal(err)
	}
	pc, err := pion.NewAPI(pion.WithMediaEngine(engine)).NewPeerConnection(pion.Configuration{})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { pc.Close() })
	if _, err := pc.AddTransceiverFromKind(pion.RTPCodecTypeVideo,
		pion.RTPTransceiverInit{Direction: pion.RTPTransceiverDirectionRecvonly}); err != nil {
		t.Fatal(err)
	}
	tracks := make(chan *pion.TrackRemote, 1)
	pc.OnTrack(func(track *pion.TrackRemote, _ *pion.RTPReceiver) { tracks <- track })
	offer, err := pc.CreateOffer(nil)
	if err != nil {
		t.Fatal(err)
	}
	gathered := pion.GatheringCompletePromise(pc)
	if err := pc.SetLocalDescription(offer); err != nil {
		t.Fatal(err)
	}
	<-gathered
	resp, err := http.Post(url+"/whep", "application/sdp", strings.NewReader(pc.LocalDescription().SDP))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	answer, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusCreated {
		t.Fatalf("whep returned %d: %s", resp.StatusCode, answer)
	}
	if err := pc.SetRemoteDescription(pion.SessionDescription{Type: pion.SDPTypeAnswer, SDP: string(answer)}); err != nil {
		t.Fatal(err)
	}
	return pc, tracks
}

func TestViewerReceivesCameraFramesAndRequestsKeyframes(t *testing.T) {
	r, events, ctx := newTestRelay(t, Options{})
	var keyframeRequests atomic.Int32
	r.SetKeyframeRequester(func() { keyframeRequests.Add(1) })
	server := httptest.NewServer(r.Handler())
	defer server.Close()

	_, tracks := connectViewer(t, server.URL)
	go func() {
		keyframe := Frame{Data: annexB([]byte{0x67, 0x42, 0xC0, 0x1F}, []byte{0x68, 0xCE, 0x3C, 0x80}, []byte{0x65, 0x88, 0x84}), Duration: 33 * time.Millisecond, Keyframe: true}
		for ctx.Err() == nil {
			r.WriteFrame(keyframe)
			time.Sleep(33 * time.Millisecond)
		}
	}()

	var track *pion.TrackRemote
	select {
	case track = <-tracks:
	case <-time.After(10 * time.Second):
		t.Fatal("viewer never received the video track")
	}
	if !strings.EqualFold(track.Codec().MimeType, pion.MimeTypeH264) {
		t.Fatalf("codec = %s", track.Codec().MimeType)
	}
	pkt, _, err := track.ReadRTP()
	if err != nil || len(pkt.Payload) == 0 {
		t.Fatalf("no media: %v", err)
	}
	status := r.Status()
	if status.State != "streaming" || status.Viewers != 1 || status.Frames == 0 {
		t.Fatalf("status = %+v", status)
	}
	deadline := time.Now().Add(5 * time.Second)
	for keyframeRequests.Load() == 0 && time.Now().Before(deadline) {
		time.Sleep(50 * time.Millisecond)
	}
	if keyframeRequests.Load() == 0 {
		t.Fatal("a newly connected viewer should trigger a camera keyframe request")
	}
	var sawStreaming bool
	for _, event := range events.events() {
		if event["event"] == "state" && event["state"] == "streaming" {
			sawStreaming = true
		}
	}
	if !sawStreaming {
		t.Fatalf("no streaming event in %v", events.events())
	}
}

func TestRejectsNonSDPAndWrongMethods(t *testing.T) {
	r, _, _ := newTestRelay(t, Options{})
	server := httptest.NewServer(r.Handler())
	defer server.Close()
	resp, _ := http.Post(server.URL+"/whep", "application/sdp", strings.NewReader("hello"))
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	resp, _ = http.Get(server.URL + "/whep")
	if resp.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	resp, _ = http.Get(server.URL + "/stop")
	if resp.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("GET /stop must not stop the session: %d", resp.StatusCode)
	}
}

func TestSessionEndsWithoutViewersOrVideo(t *testing.T) {
	r, _, ctx := newTestRelay(t, Options{IdleTimeout: 1500 * time.Millisecond, StartTimeout: time.Hour})
	go r.Watch(ctx)
	select {
	case <-ctx.Done():
		if cause := context.Cause(ctx).Error(); cause != "no viewers" {
			t.Fatalf("cause = %q", cause)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("idle session did not end")
	}

	r2, _, ctx2 := newTestRelay(t, Options{IdleTimeout: time.Hour, StartTimeout: 1500 * time.Millisecond})
	go r2.Watch(ctx2)
	select {
	case <-ctx2.Done():
		if cause := context.Cause(ctx2).Error(); !strings.Contains(cause, "did not start streaming") {
			t.Fatalf("cause = %q", cause)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("stalled session did not end")
	}
}

func TestStopEndpoint(t *testing.T) {
	r, _, ctx := newTestRelay(t, Options{})
	server := httptest.NewServer(r.Handler())
	defer server.Close()
	resp, err := http.Post(server.URL+"/stop", "", nil)
	if err != nil || resp.StatusCode != http.StatusAccepted {
		t.Fatalf("stop: %v %v", err, resp)
	}
	select {
	case <-ctx.Done():
	case <-time.After(time.Second):
		t.Fatal("stop did not cancel the session")
	}
}
