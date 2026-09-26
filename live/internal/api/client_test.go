package api

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestKiwibitWakeAndTicketRequest(t *testing.T) {
	var seenWake, seenTicket bool
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer example.jwt.value" {
			t.Errorf("bad Authorization header")
		}
		if r.Header.Get("Content-Type") != "application/json" {
			t.Errorf("bad Content-Type")
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
			return
		}
		if body["serialNumber"] != "example-camera" {
			t.Errorf("wrong serial number")
		}
		app, ok := body["app"].(map[string]any)
		if !ok || app["bundle"] != "com.kb.kiwibit" || app["tenantId"] != "kiwibit" || app["appType"] != "Android" {
			t.Errorf("wrong app metadata: %v", body["app"])
		}
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/device/wakeupDevice":
			seenWake = true
			fmt.Fprint(w, `{"result":0,"msg":"success","data":{}}`)
		case "/device/getWebrtcTicket":
			seenTicket = true
			for _, key := range []string{"supportUnlimitedWebsocket", "verifyDormancyStatus", "verify4GVIPStatus"} {
				if body[key] != true {
					t.Errorf("%s should be true", key)
				}
			}
			if body["needWakeupDevice"] != false {
				t.Errorf("needWakeupDevice should be false")
			}
			if _, exists := body["list"]; exists {
				t.Errorf("unexpected VicoHome list field")
			}
			fmt.Fprintf(w, `{"result":0,"msg":"success","data":{"id":"viewer-1","role":"viewer","signalServer":"wss://signal.example","websocketPath":"/iot-channel/connect","accessToken":"session-token","expirationTime":%d,"iceServer":[{"url":"turn:relay.example:5349","username":"user","credential":"password"}]}}`, time.Now().Add(5*time.Minute).UnixMilli())
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	client := NewClient()
	client.url = server.URL + "/device/getWebrtcTicket"
	client.wakeupURL = server.URL + "/device/wakeupDevice"
	if err := client.WakeupDevice("Bearer example.jwt.value", "example-camera"); err != nil {
		t.Fatal(err)
	}
	ticket, err := client.FetchTicket("example.jwt.value", "example-camera")
	if err != nil {
		t.Fatal(err)
	}
	if !seenWake || !seenTicket || ticket.ID != "viewer-1" || len(ticket.ICEServers) != 1 {
		t.Fatalf("unexpected results: wake=%v ticket=%v data=%+v", seenWake, seenTicket, ticket)
	}
}
