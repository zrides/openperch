package relay

import (
	"log"
	"net/http"
	"time"
)

// A subscriber receives the raw camera frames, for recording.
type subscriber struct {
	frames chan Frame
}

func (r *Relay) publish(frame Frame) {
	r.mu.Lock()
	defer r.mu.Unlock()
	for s := range r.subscribers {
		select {
		case s.frames <- frame:
		default:
			// A stalled reader loses frames rather than slowing the live view.
		}
	}
}

func (r *Relay) subscribe() *subscriber {
	s := &subscriber{frames: make(chan Frame, 128)}
	r.mu.Lock()
	r.subscribers[s] = struct{}{}
	r.mu.Unlock()
	return s
}

func (r *Relay) unsubscribe(s *subscriber) {
	r.mu.Lock()
	delete(r.subscribers, s)
	r.lastViewer = time.Now()
	r.mu.Unlock()
}

// serveStream writes the live video as an Annex-B H264 byte stream, starting
// at a keyframe, until the client disconnects or the session ends.
func (r *Relay) serveStream(w http.ResponseWriter, req *http.Request) {
	if req.Method != http.MethodGet {
		http.Error(w, "use GET", http.StatusMethodNotAllowed)
		return
	}
	s := r.subscribe()
	defer r.unsubscribe(s)
	r.requestKeyframe()
	w.Header().Set("Content-Type", "video/h264")
	w.WriteHeader(http.StatusOK)
	flusher, _ := w.(http.Flusher)
	if flusher != nil {
		flusher.Flush()
	}
	started := false
	for {
		select {
		case <-req.Context().Done():
			return
		case <-r.done:
			return
		case frame := <-s.frames:
			if !started && !frame.Keyframe {
				continue // decoders need a keyframe (with SPS/PPS) first
			}
			started = true
			if _, err := w.Write(frame.Data); err != nil {
				log.Printf("[relay] stream client gone: %v", err)
				return
			}
			if flusher != nil {
				flusher.Flush()
			}
		}
	}
}
