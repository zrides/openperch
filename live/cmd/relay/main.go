// kiwibit-relay connects to a Kiwibit camera over WebRTC and re-serves its
// video to browsers. It is started on demand by OpenPerch.
//
// Environment:
//
//	KIWIBIT_TOKEN, KIWIBIT_SN, KIWIBIT_RESOLUTION  as for kiwibit-stream
//	KIWIBIT_RELAY_ADDR      HTTP control address (default 127.0.0.1:0)
//	KIWIBIT_LIVE_UDP_PORT   one UDP port for all browser media (default: any)
//	KIWIBIT_LIVE_HOSTS      comma-separated addresses browsers should dial
//	KIWIBIT_LIVE_IDLE       seconds without viewers before ending (default 30)
//
// Stdout carries one JSON event per line for the supervisor; logs go to stderr.
package main

import (
	"context"
	"errors"
	"log"
	"net"
	"net/http"
	"os"
	ossignal "os/signal"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"vico_home/native/internal/api"
	"vico_home/native/internal/config"
	"vico_home/native/internal/domain"
	"vico_home/native/internal/relay"
	sigclient "vico_home/native/internal/signal"
	"vico_home/native/internal/viewer"
	"vico_home/native/internal/webrtc"
)

// stateHandler reports signaling progress, then defers to the proven viewer flow.
type stateHandler struct {
	domain.Handler
	relay *relay.Relay
}

func (h stateHandler) OnAuthSuccess() {
	h.relay.SetState("joining", "")
	h.Handler.OnAuthSuccess()
}

func (h stateHandler) OnPeerIn() {
	h.relay.SetState("connecting", "")
	h.Handler.OnPeerIn()
}

func envInt(name string, fallback int) int {
	if value, err := strconv.Atoi(os.Getenv(name)); err == nil && value >= 0 {
		return value
	}
	return fallback
}

func main() {
	log.SetOutput(os.Stderr)
	log.SetFlags(log.Ltime | log.Lmicroseconds)

	cfg, err := config.Load()
	if err != nil {
		log.Fatalf("[relay] %v", err)
	}
	var hosts []string
	for _, host := range strings.Split(os.Getenv("KIWIBIT_LIVE_HOSTS"), ",") {
		if host = strings.TrimSpace(host); host != "" {
			hosts = append(hosts, host)
		}
	}
	ctx, cancel := context.WithCancelCause(context.Background())
	defer cancel(nil)

	r, err := relay.New(relay.Options{
		UDPPort:      envInt("KIWIBIT_LIVE_UDP_PORT", 0),
		PublicHosts:  hosts,
		IdleTimeout:  time.Duration(envInt("KIWIBIT_LIVE_IDLE", 30)) * time.Second,
		StartTimeout: 75 * time.Second,
	}, cfg.Resolution, os.Stdout, cancel)
	if err != nil {
		log.Fatalf("[relay] %v", err)
	}

	addr := os.Getenv("KIWIBIT_RELAY_ADDR")
	if addr == "" {
		addr = "127.0.0.1:0"
	}
	listener, err := net.Listen("tcp", addr)
	if err != nil {
		log.Fatalf("[relay] listen: %v", err)
	}
	server := &http.Server{Handler: r.Handler(), ReadHeaderTimeout: 5 * time.Second}
	go server.Serve(listener)
	r.Emit(map[string]any{"event": "listening", "port": listener.Addr().(*net.TCPAddr).Port})

	signals := make(chan os.Signal, 1)
	ossignal.Notify(signals, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		select {
		case sig := <-signals:
			cancel(errors.New("stopped (" + sig.String() + ")"))
		case <-ctx.Done():
		}
	}()
	go r.Watch(ctx)

	// Filled by the startup goroutine, run once at shutdown.
	var cleanupMu sync.Mutex
	var cleanup []func()
	onShutdown := func(fn func()) {
		cleanupMu.Lock()
		cleanup = append(cleanup, fn)
		cleanupMu.Unlock()
	}
	go func() {
		client := api.NewClient()
		r.SetState("waking", "")
		if err := client.WakeupDevice(cfg.Token, cfg.SerialNumber); err != nil {
			log.Printf("[relay] wake request failed: %v", err)
		}
		r.SetState("signaling", "")
		ticket, err := client.FetchTicket(cfg.Token, cfg.SerialNumber)
		if err != nil {
			cancel(errors.New("could not get a live ticket: " + err.Error()))
			return
		}
		peer, err := webrtc.NewPeer(ticket.ICEServers, cfg.SerialNumber, cfg.Resolution)
		if err != nil {
			cancel(err)
			return
		}
		onShutdown(peer.Close)
		if err := peer.AddTransceivers(); err != nil {
			cancel(err)
			return
		}
		assembler := relay.NewAssembler(r.WriteFrame)
		peer.SetOnVideoRTP(assembler.Push)
		peer.OnFailed(func(state string) { cancel(errors.New("camera connection " + state)) })
		r.SetKeyframeRequester(peer.RequestKeyframe)

		v := viewer.New(peer, func() { cancel(errors.New("the camera left the session")) })
		sc := sigclient.NewClient(ticket, cfg.SerialNumber, cfg.Resolution, stateHandler{Handler: v, relay: r})
		onShutdown(sc.Close)
		v.SetSignaler(sc)
		sc.SetOnClosed(func(reason string) { cancel(errors.New(reason)) })
		peer.SetOnICECandidate(sc.SendICECandidate)
		if err := sc.Connect(); err != nil {
			cancel(errors.New("could not reach the signaling server: " + err.Error()))
		}
	}()

	<-ctx.Done()
	reason := context.Cause(ctx).Error()
	r.SetState("ended", reason)
	r.Close()
	cleanupMu.Lock()
	for i := len(cleanup) - 1; i >= 0; i-- {
		cleanup[i]()
	}
	cleanupMu.Unlock()
	shutdown, done := context.WithTimeout(context.Background(), 2*time.Second)
	defer done()
	server.Shutdown(shutdown)
}
