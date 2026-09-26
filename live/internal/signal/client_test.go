package signal

import (
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"vico_home/native/internal/domain"

	"github.com/gorilla/websocket"
)

type testHandler struct{ answers chan domain.SDPPayload }

func (h *testHandler) OnAuthSuccess()                                  {}
func (h *testHandler) OnPeerIn()                                       {}
func (h *testHandler) OnPeerOut()                                      {}
func (h *testHandler) OnSDPAnswer(p domain.SDPPayload)                 { h.answers <- p }
func (h *testHandler) OnRemoteICECandidate(domain.ICECandidatePayload) {}

func TestKiwibitSignalingFramesAndCandidateOrder(t *testing.T) {
	frames := make(chan map[string]any, 4)
	sendAnswer := make(chan struct{})
	errors := make(chan error, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/iot-channel/connect" {
			http.NotFound(w, r)
			return
		}
		conn, err := (&websocket.Upgrader{}).Upgrade(w, r, nil)
		if err != nil {
			errors <- err
			return
		}
		defer conn.Close()
		for i := 0; i < 3; i++ {
			var frame map[string]any
			if err := conn.ReadJSON(&frame); err != nil {
				errors <- err
				return
			}
			frames <- frame
		}
		select {
		case <-sendAnswer:
		case <-r.Context().Done():
			return
		}
		answer, _ := json.Marshal(domain.SDPPayload{Type: "answer", SDP: "v=0\r\n"})
		if err := conn.WriteJSON(message{Method: "TRANSMIT", MessageType: "SDP_ANSWER", MessagePayload: base64.StdEncoding.EncodeToString(answer)}); err != nil {
			errors <- err
			return
		}
		var candidate map[string]any
		if err := conn.ReadJSON(&candidate); err != nil {
			errors <- err
			return
		}
		frames <- candidate
	}))
	defer server.Close()
	handler := &testHandler{answers: make(chan domain.SDPPayload, 1)}
	client := NewClient(&domain.Ticket{ID: "viewer-1", Role: "viewer", GroupID: "group-1", TraceID: "trace-1", AccessToken: "session-secret", SignalServer: strings.Replace(server.URL, "http://", "ws://", 1), WebsocketPath: "/iot-channel/connect", SignalPingInterval: 30}, "example-camera", "1280x720", handler)
	if err := client.Connect(); err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	client.SendJoinLive()
	client.SendSDPOffer("v=0\r\n")
	client.SendICECandidate("0", 0, "candidate:1 1 udp 1 192.0.2.1 5000 typ host")
	client.candidateMu.Lock()
	pending := len(client.pendingCandidates)
	client.candidateMu.Unlock()
	if pending != 1 {
		t.Fatalf("candidate sent before SDP answer: pending=%d", pending)
	}
	for _, want := range []string{"AUTH", "JOIN_LIVE", "TRANSMIT"} {
		select {
		case frame := <-frames:
			if frame["method"] != want {
				t.Fatalf("method=%v, want %s", frame["method"], want)
			}
			switch want {
			case "AUTH":
				if frame["version"] != "0.0.1" || frame["accessToken"] != "session-secret" {
					t.Fatalf("incorrect AUTH frame")
				}
			case "JOIN_LIVE":
				if frame["supportRtcConnectAcceleration"] != float64(0) || frame["recipientClientId"] != "example-camera" || frame["traceparent"] == "" {
					t.Fatalf("incorrect JOIN_LIVE frame: %v", frame)
				}
			case "TRANSMIT":
				if frame["messageType"] != "SDP_OFFER" || frame["sdpMode"] != float64(0) || frame["resolution"] != "1280x720" {
					t.Fatalf("incorrect SDP_OFFER frame: %v", frame)
				}
				if _, found := frame["mode"]; found {
					t.Fatalf("unexpected VicoHome mode field")
				}
			}
		case err := <-errors:
			t.Fatal(err)
		case <-time.After(3 * time.Second):
			t.Fatal("timed out waiting for signaling frame")
		}
	}
	close(sendAnswer)
	select {
	case answer := <-handler.answers:
		if answer.SDP != "v=0\r\n" {
			t.Fatalf("wrong SDP answer")
		}
	case <-time.After(3 * time.Second):
		t.Fatal("timed out waiting for SDP answer")
	}
	select {
	case frame := <-frames:
		if frame["messageType"] != "ICE_CANDIDATE" {
			t.Fatalf("wrong candidate frame: %v", frame)
		}
	case err := <-errors:
		t.Fatal(err)
	case <-time.After(3 * time.Second):
		t.Fatal("timed out waiting for buffered candidate")
	}
}
