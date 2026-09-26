package config

import (
	"fmt"
	"os"

	"github.com/joho/godotenv"
)

// Config holds the application configuration.
type Config struct {
	Token        string
	SerialNumber string
	Resolution   string
}

const DefaultResolution = "1280x720"

func ValidResolution(value string) bool {
	switch value {
	case "1280x720", "1920x1080", "3840x2160":
		return true
	default:
		return false
	}
}

// Load reads configuration from a .env file (if present) and environment variables.
// Environment variables take precedence over .env values.
func Load() (*Config, error) {
	// godotenv.Load does not overwrite existing env vars
	_ = godotenv.Load()

	token := os.Getenv("KIWIBIT_TOKEN")
	if token == "" {
		return nil, fmt.Errorf("KIWIBIT_TOKEN environment variable is required")
	}

	sn := os.Getenv("KIWIBIT_SN")
	if sn == "" {
		return nil, fmt.Errorf("KIWIBIT_SN environment variable is required")
	}
	resolution := os.Getenv("KIWIBIT_RESOLUTION")
	if resolution == "" {
		resolution = DefaultResolution
	}
	if !ValidResolution(resolution) {
		return nil, fmt.Errorf("KIWIBIT_RESOLUTION must be 1280x720, 1920x1080, or 3840x2160")
	}

	return &Config{
		Token:        token,
		SerialNumber: sn,
		Resolution:   resolution,
	}, nil
}
