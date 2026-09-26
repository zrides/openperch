package relay

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/pion/rtcp"
	pion "github.com/pion/webrtc/v4"
	"github.com/pion/webrtc/v4/pkg/media"
)

const maxViewers = 8

// Options configure how browsers reach the relay.
type Options struct {
	// UDPPort, when non-zero, carries every browser connection on one UDP port
	// so a container needs a single published port.
	UDPPort int
	// PublicHosts replace the host candidate addresses browsers are given, for
	// example 127.0.0.1 and the LAN address when the relay runs in Docker.
	PublicHosts []string
	// IdleTimeout ends the session after this long without viewers.
	IdleTimeout time.Duration
	// StartTimeout ends the session if the camera sends no video in time.
	StartTimeout time.Duration
}

// Status is what GET /status reports.
type Status struct {
	State      string `json:"state"`
	Detail     string `json:"detail,omitempty"`
	Resolution string `json:"resolution"`
	Viewers    int    `json:"viewers"`
	Frames     uint64 `json:"frames"`
	Uptime     int64  `json:"uptime_seconds"`
}

// Relay forwards one camera stream to any number of browser peers.
type Relay struct {
	api        *pion.API
	track      *pion.TrackLocalStaticSample
	opts       Options
	resolution string
	events     io.Writer
	cancel     context.CancelCauseFunc
	keyframe   func()

	mu         sync.Mutex
	state      string
	detail     string
	viewers    map[*pion.PeerConnection]struct{}
	frames     uint64
	started    time.Time
	lastViewer time.Time
	lastPLI    time.Time
	eventsMu   sync.Mutex
}

func New(opts Options, resolution string, events io.Writer, cancel context.CancelCauseFunc) (*Relay, error) {
	engine := &pion.MediaEngine{}
	if err := engine.RegisterDefaultCodecs(); err != nil {
		return nil, fmt.Errorf("register codecs: %w", err)
	}
	settings := pion.SettingEngine{}
	settings.SetNetworkTypes([]pion.NetworkType{pion.NetworkTypeUDP4})
	if opts.UDPPort > 0 {
		conn, err := net.ListenUDP("udp4", &net.UDPAddr{Port: opts.UDPPort})
		if err != nil {
			return nil, fmt.Errorf("listen on UDP %d: %w", opts.UDPPort, err)
		}
		settings.SetICEUDPMux(pion.NewICEUDPMux(nil, conn))
	}
	if len(opts.PublicHosts) > 0 {
		// pion accepts one address here (or ext/int pairs, but the container's
		// internal address isn't known). The others are added to each answer
		// by withExtraHosts; the shared UDP port receives on every address.
		settings.SetNAT1To1IPs(opts.PublicHosts[:1], pion.ICECandidateTypeHost)
	}
	track, err := pion.NewTrackLocalStaticSample(
		pion.RTPCodecCapability{MimeType: pion.MimeTypeH264, ClockRate: 90000}, "video", "kiwibit-live")
	if err != nil {
		return nil, fmt.Errorf("create track: %w", err)
	}
	now := time.Now()
	return &Relay{
		api:        pion.NewAPI(pion.WithMediaEngine(engine), pion.WithSettingEngine(settings)),
		track:      track,
		opts:       opts,
		resolution: resolution,
		events:     events,
		cancel:     cancel,
		keyframe:   func() {},
		viewers:    map[*pion.PeerConnection]struct{}{},
		started:    now,
		lastViewer: now,
		state:      "starting",
	}, nil
}

// SetKeyframeRequester connects browser keyframe requests to the camera.
func (r *Relay) SetKeyframeRequester(fn func()) {
	r.keyframe = fn
}

// Emit writes one JSON event line for the supervising process.
func (r *Relay) Emit(event map[string]any) {
	r.eventsMu.Lock()
	defer r.eventsMu.Unlock()
	data, _ := json.Marshal(event)
	fmt.Fprintln(r.events, string(data))
}

func (r *Relay) SetState(state, detail string) {
	r.mu.Lock()
	changed := r.state != state || r.detail != detail
	r.state, r.detail = state, detail
	r.mu.Unlock()
	if changed {
		log.Printf("[relay] state %s %s", state, detail)
		r.Emit(map[string]any{"event": "state", "state": state, "detail": detail})
	}
}

// WriteFrame sends one camera frame to every connected browser.
func (r *Relay) WriteFrame(frame Frame) {
	if err := r.track.WriteSample(media.Sample{Data: frame.Data, Duration: frame.Duration}); err != nil {
		log.Printf("[relay] write sample: %v", err)
		return
	}
	r.mu.Lock()
	r.frames++
	first := r.frames == 1
	r.mu.Unlock()
	if first {
		r.SetState("streaming", "")
	}
}

func (r *Relay) Status() Status {
	r.mu.Lock()
	defer r.mu.Unlock()
	return Status{State: r.state, Detail: r.detail, Resolution: r.resolution, Viewers: len(r.viewers),
		Frames: r.frames, Uptime: int64(time.Since(r.started).Seconds())}
}

func (r *Relay) requestKeyframe() {
	r.mu.Lock()
	if time.Since(r.lastPLI) < 500*time.Millisecond {
		r.mu.Unlock()
		return
	}
	r.lastPLI = time.Now()
	r.mu.Unlock()
	r.keyframe()
}

// Watch ends the session when nobody is watching or the camera never streams.
func (r *Relay) Watch(ctx context.Context) {
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			r.mu.Lock()
			idle := len(r.viewers) == 0 && time.Since(r.lastViewer) > r.opts.IdleTimeout
			stalled := r.frames == 0 && time.Since(r.started) > r.opts.StartTimeout
			r.mu.Unlock()
			if idle {
				r.cancel(fmt.Errorf("no viewers"))
			} else if stalled {
				r.cancel(fmt.Errorf("the camera did not start streaming"))
			}
		}
	}
}

// Handler serves POST /whep (SDP offer in, SDP answer out), GET /status and POST /stop.
func (r *Relay) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/whep", func(w http.ResponseWriter, req *http.Request) {
		if req.Method != http.MethodPost {
			http.Error(w, "POST an SDP offer", http.StatusMethodNotAllowed)
			return
		}
		offer, err := io.ReadAll(io.LimitReader(req.Body, 64<<10))
		if err != nil || !strings.HasPrefix(strings.TrimSpace(string(offer)), "v=0") {
			http.Error(w, "body must be an SDP offer", http.StatusBadRequest)
			return
		}
		answer, err := r.addViewer(string(offer))
		if err != nil {
			log.Printf("[relay] viewer rejected: %v", err)
			http.Error(w, err.Error(), http.StatusServiceUnavailable)
			return
		}
		w.Header().Set("Content-Type", "application/sdp")
		w.WriteHeader(http.StatusCreated)
		io.WriteString(w, answer)
	})
	mux.HandleFunc("/status", func(w http.ResponseWriter, req *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(r.Status())
	})
	mux.HandleFunc("/stop", func(w http.ResponseWriter, req *http.Request) {
		if req.Method != http.MethodPost {
			http.Error(w, "use POST", http.StatusMethodNotAllowed)
			return
		}
		r.cancel(fmt.Errorf("stopped"))
		w.WriteHeader(http.StatusAccepted)
	})
	return mux
}

func (r *Relay) addViewer(offer string) (string, error) {
	r.mu.Lock()
	if r.state == "ended" {
		r.mu.Unlock()
		return "", fmt.Errorf("the live session has ended")
	}
	if len(r.viewers) >= maxViewers {
		r.mu.Unlock()
		return "", fmt.Errorf("too many viewers")
	}
	r.mu.Unlock()

	log.Printf("[relay] viewer offer candidates: %s", summarizeCandidates(offer))
	pc, err := r.api.NewPeerConnection(pion.Configuration{})
	if err != nil {
		return "", fmt.Errorf("create peer connection: %w", err)
	}
	fail := func(err error) (string, error) {
		pc.Close()
		return "", err
	}
	sender, err := pc.AddTrack(r.track)
	if err != nil {
		return fail(fmt.Errorf("add track: %w", err))
	}
	// Browser feedback: forward keyframe requests to the camera.
	go func() {
		for {
			packets, _, err := sender.ReadRTCP()
			if err != nil {
				return
			}
			for _, packet := range packets {
				switch packet.(type) {
				case *rtcp.PictureLossIndication, *rtcp.FullIntraRequest:
					r.requestKeyframe()
				}
			}
		}
	}()
	pc.OnConnectionStateChange(func(state pion.PeerConnectionState) {
		log.Printf("[relay] viewer connection %s", state.String())
		switch state {
		case pion.PeerConnectionStateConnected:
			r.requestKeyframe()
		case pion.PeerConnectionStateFailed, pion.PeerConnectionStateClosed, pion.PeerConnectionStateDisconnected:
			r.removeViewer(pc)
		}
	})
	if err := pc.SetRemoteDescription(pion.SessionDescription{Type: pion.SDPTypeOffer, SDP: offer}); err != nil {
		return fail(fmt.Errorf("invalid offer: %w", err))
	}
	answer, err := pc.CreateAnswer(nil)
	if err != nil {
		return fail(fmt.Errorf("create answer: %w", err))
	}
	gathered := pion.GatheringCompletePromise(pc)
	if err := pc.SetLocalDescription(answer); err != nil {
		return fail(fmt.Errorf("set local description: %w", err))
	}
	select {
	case <-gathered:
	case <-time.After(5 * time.Second):
		return fail(fmt.Errorf("timed out gathering ICE candidates"))
	}
	r.mu.Lock()
	r.viewers[pc] = struct{}{}
	r.lastViewer = time.Now()
	r.mu.Unlock()
	r.Emit(map[string]any{"event": "viewers", "count": r.Status().Viewers})
	return withExtraHosts(pc.LocalDescription().SDP, r.opts.PublicHosts), nil
}

// withExtraHosts copies each UDP host candidate advertised on hosts[0] to the
// remaining hosts, at slightly lower priority, so a browser can use whichever
// address it can reach (Firefox skips 127.0.0.1; phones need the LAN address).
func withExtraHosts(sdp string, hosts []string) string {
	if len(hosts) < 2 {
		return sdp
	}
	lines := strings.SplitAfter(sdp, "\n")
	out := make([]string, 0, len(lines)+len(hosts))
	for _, line := range lines {
		out = append(out, line)
		body := strings.TrimRight(line, "\r\n")
		if !strings.HasPrefix(body, "a=candidate:") {
			continue
		}
		// candidate:<foundation> <component> <transport> <priority> <address> <port> typ <type>
		fields := strings.Fields(strings.TrimPrefix(body, "a=candidate:"))
		if len(fields) < 8 || !strings.EqualFold(fields[2], "udp") || fields[4] != hosts[0] || fields[7] != "host" {
			continue
		}
		priority, err := strconv.ParseUint(fields[3], 10, 32)
		if err != nil {
			continue
		}
		ending := line[len(body):]
		for i, host := range hosts[1:] {
			extra := append([]string(nil), fields...)
			extra[0] = fields[0] + "x" + strconv.Itoa(i+1)
			if priority > uint64(i+1) {
				extra[3] = strconv.FormatUint(priority-uint64(i+1), 10)
			}
			extra[4] = host
			out = append(out, "a=candidate:"+strings.Join(extra, " ")+ending)
		}
	}
	return strings.Join(out, "")
}

// summarizeCandidates describes a browser's ICE candidates without addresses,
// e.g. "udp/host/mdns×2 tcp/host/mdns×1": enough to see why a viewer can't connect.
func summarizeCandidates(sdp string) string {
	counts := map[string]int{}
	var order []string
	for _, line := range strings.Split(sdp, "\n") {
		line = strings.TrimSpace(line)
		if !strings.HasPrefix(line, "a=candidate:") {
			continue
		}
		// candidate:<foundation> <component> <transport> <priority> <address> <port> typ <type> ...
		fields := strings.Fields(strings.TrimPrefix(line, "a=candidate:"))
		if len(fields) < 8 {
			continue
		}
		address := fields[4]
		kind := "ipv4"
		switch {
		case strings.HasSuffix(address, ".local"):
			kind = "mdns"
		case strings.Contains(address, ":"):
			kind = "ipv6"
		case strings.HasPrefix(address, "127."):
			kind = "loopback"
		}
		key := strings.ToLower(fields[2]) + "/" + fields[7] + "/" + kind
		if counts[key] == 0 {
			order = append(order, key)
		}
		counts[key]++
	}
	if len(order) == 0 {
		return "none (the browser offered no ICE candidates)"
	}
	parts := make([]string, len(order))
	for i, key := range order {
		parts[i] = fmt.Sprintf("%s×%d", key, counts[key])
	}
	return strings.Join(parts, " ")
}

func (r *Relay) removeViewer(pc *pion.PeerConnection) {
	r.mu.Lock()
	_, known := r.viewers[pc]
	delete(r.viewers, pc)
	r.lastViewer = time.Now()
	count := len(r.viewers)
	r.mu.Unlock()
	if known {
		go pc.Close()
		r.Emit(map[string]any{"event": "viewers", "count": count})
	}
}

// Close disconnects every viewer.
func (r *Relay) Close() {
	r.mu.Lock()
	viewers := make([]*pion.PeerConnection, 0, len(r.viewers))
	for pc := range r.viewers {
		viewers = append(viewers, pc)
	}
	r.viewers = map[*pion.PeerConnection]struct{}{}
	r.mu.Unlock()
	for _, pc := range viewers {
		pc.Close()
	}
}
