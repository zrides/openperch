package signal

import (
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log"
	"net/url"
	"sync"
	"time"

	"vico_home/native/internal/domain"

	"github.com/gorilla/websocket"
)

// message is the generic WebSocket message envelope.
type message struct {
	Method                        string `json:"method"`
	Code                          *int   `json:"code,omitempty"`
	Message                       string `json:"message,omitempty"`
	ClientType                    string `json:"clientType,omitempty"`
	ClientID                      string `json:"clientId,omitempty"`
	Status                        string `json:"status,omitempty"`
	AccessToken                   string `json:"accessToken,omitempty"`
	ID                            string `json:"id,omitempty"`
	Role                          string `json:"role,omitempty"`
	Name                          string `json:"name,omitempty"`
	Group                         string `json:"group,omitempty"`
	TraceID                       string `json:"traceId,omitempty"`
	Traceparent                   string `json:"traceparent,omitempty"`
	SupportRTCConnectAcceleration *int   `json:"supportRtcConnectAcceleration,omitempty"`
	RecipientClientID             string `json:"recipientClientId,omitempty"`
	SenderClientID                string `json:"senderClientId,omitempty"`
	SessionID                     string `json:"sessionId,omitempty"`
	MessageType                   string `json:"messageType,omitempty"`
	MessagePayload                any    `json:"messagePayload,omitempty"`
	ViewerType                    string `json:"viewerType,omitempty"`
	Resolution                    string `json:"resolution,omitempty"`
	Version                       string `json:"version,omitempty"`
	SDPMode                       *int   `json:"sdpMode,omitempty"`
	Timestamp                     int64  `json:"timestamp,omitempty"`
	Reason                        int    `json:"reason,omitempty"`
}

// Client manages the WebSocket connection to the signaling server.
type Client struct {
	conn       *websocket.Conn
	ticket     *domain.Ticket
	serial     string
	resolution string
	sessionID  string
	handler    domain.Handler

	mu                sync.Mutex
	closed            chan struct{}
	candidateMu       sync.Mutex
	pendingCandidates []message
	answerReceived    bool
}

// NewClient creates a new signaling client.
func NewClient(ticket *domain.Ticket, serialNumber, resolution string, handler domain.Handler) *Client {
	sessionID := fmt.Sprintf("Android-%s-%d", ticket.ID, time.Now().UnixMilli())
	return &Client{
		ticket:     ticket,
		serial:     serialNumber,
		resolution: resolution,
		sessionID:  sessionID,
		handler:    handler,
		closed:     make(chan struct{}),
	}
}

// Connect dials the signaling WebSocket and starts the read loop.
func (c *Client) Connect() error {
	u, err := url.Parse(c.ticket.SignalServer)
	if err != nil {
		return fmt.Errorf("parse signal server: %w", err)
	}
	u.Path = c.ticket.WebsocketPath

	log.Printf("[signal] connecting to %s", u.String())

	conn, _, err := websocket.DefaultDialer.Dial(u.String(), nil)
	if err != nil {
		return fmt.Errorf("websocket dial: %w", err)
	}
	c.conn = conn

	c.sendAuth()

	go c.readLoop()
	go c.pingLoop()

	return nil
}

// Close shuts down the WebSocket connection.
func (c *Client) Close() {
	select {
	case <-c.closed:
		return
	default:
		close(c.closed)
	}
	if c.conn != nil {
		c.conn.Close()
	}
}

func (c *Client) sendJSON(msg any) {
	c.mu.Lock()
	defer c.mu.Unlock()

	data, err := json.Marshal(msg)
	if err != nil {
		log.Printf("[signal] marshal error: %v", err)
		return
	}
	if m, ok := msg.(message); ok {
		if m.MessageType != "ICE_CANDIDATE" {
			log.Printf("[signal] sending %s %s", m.Method, m.MessageType)
		}
	}
	if err := c.conn.WriteMessage(websocket.TextMessage, data); err != nil {
		log.Printf("[signal] write error: %v", err)
	}
}

func (c *Client) sendAuth() {
	c.sendJSON(message{
		Method:      "AUTH",
		Version:     "0.0.1",
		ClientType:  "app",
		Status:      "normal",
		AccessToken: c.ticket.AccessToken,
		ID:          c.ticket.ID,
	})
}

// SendJoinLive sends the JOIN_LIVE message to the signaling server.
func (c *Client) SendJoinLive() {
	role := c.ticket.Role
	if role == "" {
		role = "viewer"
	}
	group := c.ticket.GroupID
	if group == "" {
		group = c.serial
	}
	zero := 0
	c.sendJSON(message{
		Method:                        "JOIN_LIVE",
		Role:                          role,
		Name:                          c.ticket.ID,
		Group:                         group,
		TraceID:                       c.ticket.TraceID,
		Traceparent:                   newTraceparent(),
		RecipientClientID:             c.serial,
		SupportRTCConnectAcceleration: &zero,
	})
}

func newTraceparent() string {
	var traceID [16]byte
	var spanID [8]byte
	if _, err := rand.Read(traceID[:]); err != nil {
		return ""
	}
	if _, err := rand.Read(spanID[:]); err != nil {
		return ""
	}
	return "00-" + hex.EncodeToString(traceID[:]) + "-" + hex.EncodeToString(spanID[:]) + "-01"
}

// SendSDPOffer sends the SDP offer via TRANSMIT.
func (c *Client) SendSDPOffer(sdp string) {
	zero := 0
	payload := domain.SDPPayload{Type: "offer", SDP: sdp}
	payloadJSON, _ := json.Marshal(payload)
	encoded := base64.StdEncoding.EncodeToString(payloadJSON)

	c.sendJSON(message{
		Method:            "TRANSMIT",
		MessageType:       "SDP_OFFER",
		MessagePayload:    encoded,
		RecipientClientID: c.serial,
		SenderClientID:    c.ticket.ID,
		SessionID:         c.sessionID,
		ViewerType:        "a4x_sdk",
		Resolution:        c.resolution,
		Version:           "0.0.1",
		SDPMode:           &zero,
	})
}

// SendICECandidate sends a local ICE candidate via TRANSMIT.
func (c *Client) SendICECandidate(sdpMid string, sdpMLineIndex int, candidate string) {
	payload := domain.ICECandidatePayload{
		SDPMid:        sdpMid,
		SDPMLineIndex: sdpMLineIndex,
		Candidate:     candidate,
	}
	payloadJSON, _ := json.Marshal(payload)
	encoded := base64.StdEncoding.EncodeToString(payloadJSON)

	msg := message{
		Method:            "TRANSMIT",
		MessageType:       "ICE_CANDIDATE",
		MessagePayload:    encoded,
		RecipientClientID: c.serial,
		SenderClientID:    c.ticket.ID,
		SessionID:         c.sessionID,
		Version:           "0.0.1",
	}
	c.candidateMu.Lock()
	defer c.candidateMu.Unlock()
	if !c.answerReceived {
		c.pendingCandidates = append(c.pendingCandidates, msg)
		return
	}
	c.sendJSON(msg)
}

func (c *Client) flushCandidates() {
	c.candidateMu.Lock()
	defer c.candidateMu.Unlock()
	c.answerReceived = true
	for _, msg := range c.pendingCandidates {
		c.sendJSON(msg)
	}
	c.pendingCandidates = nil
}

func decodePayload(value any, dst any) error {
	switch payload := value.(type) {
	case string:
		if len(payload) > 0 && payload[0] == '{' {
			return json.Unmarshal([]byte(payload), dst)
		}
		decoded, err := base64.StdEncoding.DecodeString(payload)
		if err != nil {
			return err
		}
		return json.Unmarshal(decoded, dst)
	case map[string]any:
		encoded, err := json.Marshal(payload)
		if err != nil {
			return err
		}
		return json.Unmarshal(encoded, dst)
	default:
		return fmt.Errorf("unsupported signaling payload type %T", value)
	}
}

func (c *Client) readLoop() {
	defer c.Close()

	for {
		select {
		case <-c.closed:
			return
		default:
		}

		_, data, err := c.conn.ReadMessage()
		if err != nil {
			select {
			case <-c.closed:
				return
			default:
				log.Printf("[signal] read error: %v", err)
				return
			}
		}

		var msg message
		if err := json.Unmarshal(data, &msg); err != nil {
			log.Printf("[signal] unmarshal error: %v", err)
			continue
		}
		if msg.Method != "TRANSMIT_RESPONSE" && msg.MessageType != "ICE_CANDIDATE" {
			log.Printf("[signal] received %s %s", msg.Method, msg.MessageType)
		}

		c.dispatch(msg)
	}
}

func (c *Client) dispatch(msg message) {
	switch msg.Method {
	case "AUTH_RESPONSE":
		if msg.Code != nil && *msg.Code == 0 {
			log.Printf("[signal] auth successful")
			c.handler.OnAuthSuccess()
		} else {
			code := -1
			if msg.Code != nil {
				code = *msg.Code
			}
			log.Printf("[signal] auth failed: code=%d", code)
		}

	case "JOIN_LIVE_RESPONSE":
		if msg.Code != nil {
			log.Printf("[signal] join_live response: code=%d", *msg.Code)
		}

	case "WEBRTC_CONFIG":
		// The fresh ticket already includes TURN servers used by this client.

	case "PEER_IN":
		log.Printf("[signal] camera peer joined")
		c.handler.OnPeerIn()

	case "PEER_OUT":
		log.Printf("[signal] camera peer left")
		c.handler.OnPeerOut()

	case "TRANSMIT":
		switch msg.MessageType {
		case "SDP_ANSWER":
			var sdp domain.SDPPayload
			if err := decodePayload(msg.MessagePayload, &sdp); err != nil {
				log.Printf("[signal] decode SDP_ANSWER: %v", err)
				return
			}
			log.Printf("[signal] received SDP answer")
			c.handler.OnSDPAnswer(sdp)
			c.flushCandidates()

		case "ICE_CANDIDATE":
			var candidate domain.ICECandidatePayload
			if err := decodePayload(msg.MessagePayload, &candidate); err != nil {
				log.Printf("[signal] decode ICE_CANDIDATE: %v", err)
				return
			}
			log.Printf("[signal] received remote ICE candidate")
			c.handler.OnRemoteICECandidate(candidate)
		}

	case "TRANSMIT_RESPONSE", "RESPONSE":
		// no-op

	default:
		log.Printf("[signal] unhandled method: %s", msg.Method)
	}
}

func (c *Client) pingLoop() {
	interval := c.ticket.SignalPingInterval
	if interval <= 0 {
		interval = 2
	}
	ticker := time.NewTicker(time.Duration(interval) * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-c.closed:
			return
		case <-ticker.C:
			c.mu.Lock()
			err := c.conn.WriteControl(
				websocket.PingMessage,
				[]byte{},
				time.Now().Add(5*time.Second),
			)
			c.mu.Unlock()
			if err != nil {
				select {
				case <-c.closed:
					return
				default:
					log.Printf("[signal] ping error: %v", err)
					return
				}
			}
		}
	}
}
