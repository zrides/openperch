package api

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"vico_home/native/internal/domain"
)

const ticketURL = "https://api-us.kiwibit.com/device/getWebrtcTicket"
const wakeupURL = "https://api-us.kiwibit.com/device/wakeupDevice"

type ticketRequest struct {
	SerialNumber              string      `json:"serialNumber"`
	CountryNo                 string      `json:"countryNo"`
	Language                  string      `json:"language"`
	SupportUnlimitedWebsocket bool        `json:"supportUnlimitedWebsocket"`
	VerifyDormancyStatus      bool        `json:"verifyDormancyStatus"`
	Verify4GVIPStatus         bool        `json:"verify4GVIPStatus"`
	NeedWakeupDevice          bool        `json:"needWakeupDevice"`
	App                       appMetadata `json:"app"`
}

type appMetadata struct {
	VersionName string `json:"versionName"`
	Bundle      string `json:"bundle"`
	ChannelID   int    `json:"channelId"`
	AppName     string `json:"appName"`
	TimeZone    string `json:"timeZone"`
	TenantID    string `json:"tenantId"`
	Version     int    `json:"version"`
	AppType     string `json:"appType"`
}

type ticketResponse struct {
	Result int           `json:"result"`
	Msg    string        `json:"msg"`
	Data   domain.Ticket `json:"data"`
}

type wakeupRequest struct {
	SerialNumber string      `json:"serialNumber"`
	CountryNo    string      `json:"countryNo"`
	Language     string      `json:"language"`
	App          appMetadata `json:"app"`
}

func currentApp() appMetadata {
	return appMetadata{
		VersionName: "3022",
		Bundle:      "com.kb.kiwibit",
		ChannelID:   1000,
		TimeZone:    "America/New_York",
		AppName:     "Kiwibit",
		TenantID:    "kiwibit",
		Version:     3022,
		AppType:     "Android",
	}
}

// Client fetches WebRTC tickets from the Kiwibit API.
type Client struct {
	url        string
	wakeupURL  string
	httpClient *http.Client
}

// NewClient creates an API client.
func NewClient() *Client {
	return &Client{url: ticketURL, wakeupURL: wakeupURL, httpClient: &http.Client{Timeout: 15 * time.Second}}
}

func authHeader(jwt string) string {
	jwt = strings.TrimSpace(jwt)
	if len(jwt) >= 7 && strings.EqualFold(jwt[:7], "Bearer ") {
		jwt = strings.TrimSpace(jwt[7:])
	}
	return "Bearer " + jwt
}

// WakeupDevice asks a sleeping camera to come online before joining live.
func (c *Client) WakeupDevice(jwt, serialNumber string) error {
	body, err := json.Marshal(wakeupRequest{
		SerialNumber: serialNumber,
		CountryNo:    "US",
		Language:     "en",
		App:          currentApp(),
	})
	if err != nil {
		return fmt.Errorf("marshal wake request: %w", err)
	}
	req, err := http.NewRequest("POST", c.wakeupURL, bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("create wake request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", authHeader(jwt))
	resp, err := c.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("wake request: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("wake request returned HTTP %d", resp.StatusCode)
	}
	var result struct {
		Result int    `json:"result"`
		Msg    string `json:"msg"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return fmt.Errorf("decode wake response: %w", err)
	}
	if result.Result != 0 {
		return fmt.Errorf("wake request failed (result=%d): %s", result.Result, result.Msg)
	}
	return nil
}

// FetchTicket obtains fresh signaling credentials and ICE servers.
func (c *Client) FetchTicket(jwt, serialNumber string) (*domain.Ticket, error) {
	req := ticketRequest{
		SerialNumber:              serialNumber,
		CountryNo:                 "US",
		Language:                  "en",
		SupportUnlimitedWebsocket: true,
		VerifyDormancyStatus:      true,
		Verify4GVIPStatus:         true,
		NeedWakeupDevice:          false,
		App:                       currentApp(),
	}

	body, err := json.Marshal(req)
	if err != nil {
		return nil, fmt.Errorf("marshal ticket request: %w", err)
	}

	httpReq, err := http.NewRequest("POST", c.url, bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("create http request: %w", err)
	}
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("Authorization", authHeader(jwt))

	resp, err := c.httpClient.Do(httpReq)
	if err != nil {
		return nil, fmt.Errorf("http request: %w", err)
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("read response: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("ticket request returned HTTP %d", resp.StatusCode)
	}

	var ticketResp ticketResponse
	if err := json.Unmarshal(respBody, &ticketResp); err != nil {
		return nil, fmt.Errorf("unmarshal response: %w", err)
	}

	if ticketResp.Result != 0 {
		return nil, fmt.Errorf("API error (result=%d): %s", ticketResp.Result, ticketResp.Msg)
	}
	if ticketResp.Data.SignalServer == "" || ticketResp.Data.WebsocketPath == "" || ticketResp.Data.AccessToken == "" {
		return nil, fmt.Errorf("ticket response is missing signaling details")
	}
	if ticketResp.Data.ExpirationTime > 0 && ticketResp.Data.ExpirationTime <= time.Now().UnixMilli() {
		return nil, fmt.Errorf("ticket response has expired")
	}

	return &ticketResp.Data, nil
}
