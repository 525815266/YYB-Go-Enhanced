package qr

// ScanChannel is one way to authorize an account. The console builds every
// channel picker from this list (the scan page, multi-channel scanning, the
// account cards, the settings page), so a new channel shows up everywhere
// without touching a page.
//
// Adding a channel takes, besides its entry here:
//   - OAuthURL support for the product in this package;
//   - the channel's accounts recognised by (*store.WechatAccount).Public, which
//     sets scan_product from the openid prefix or the credentials' provider marker;
//   - a badge image at resource/static/images/scan-<Badge>.png;
//   - the same entry in the built-in list of resource/static/js/scan-channel.js,
//     which the public one-time link page uses because it cannot read the
//     authenticated /api/scan-channels (a test fails when the two differ).
type ScanChannel struct {
	// ID is the product value the QR endpoints take and the store reports as
	// scan_product.
	ID string `json:"id"`
	// Name is the short label shown on cards and buttons.
	Name string `json:"name"`
	// Badge names the badge image: /static/images/scan-<Badge>.png.
	Badge       string `json:"badge"`
	Description string `json:"description"`
}

var scanChannels = []ScanChannel{
	{ID: "appstore", Name: "YYB", Badge: "yyb", Description: "应用宝"},
	{ID: "pcmgr", Name: "PCMgr", Badge: "pcmgr", Description: "腾讯电脑管家"},
	{ID: "gameassist", Name: "GameAssist", Badge: "gameassist", Description: "腾讯手游助手"},
	{ID: "ldplayer", Name: "LDPlayer", Badge: "ldplayer", Description: "雷电模拟器（实验）"},
	{ID: "qbbrowser", Name: "QQ浏览器", Badge: "qbbrowser", Description: "QQ浏览器独立扫码"},
}

// ScanChannels returns the channels in display order. The first one is the
// default channel.
func ScanChannels() []ScanChannel {
	return append([]ScanChannel(nil), scanChannels...)
}

// IsScanChannel reports whether id names a channel that can be scanned.
func IsScanChannel(id string) bool {
	for _, channel := range scanChannels {
		if channel.ID == id {
			return true
		}
	}
	return false
}
