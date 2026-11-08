package protocol

import (
	"errors"
	"fmt"

	"google.golang.org/protobuf/encoding/protowire"
)

var ErrLoginCodeMalformed = errors.New("wx.login returned a malformed response")

// A decrypted business rejection is different from an absent code, a failed
// transport, or a rejected credential. Keep only its stage and numeric status.
type LoginCodeRejectedError struct {
	Stage string
	Code  int32
}

func (e *LoginCodeRejectedError) Error() string {
	return fmt.Sprintf("wx.login %s rejected: code=%d", e.Stage, e.Code)
}

func IsBusinessResponseRejection(err error) bool {
	var login *LoginCodeRejectedError
	var h5 *ErrH5OAuthBusinessRejected
	return errors.As(err, &login) || errors.As(err, &h5)
}

type loginResponseField struct {
	wire protowire.Type
	n    uint64
	b    []byte
}

// Only the fixed envelope -> field 2 -> JSLoginResponse path is supported.
// Do not search arbitrary nested bytes for a code or interpret error text.
func loginResponseFields(raw []byte) (map[protowire.Number]loginResponseField, error) {
	fields := make(map[protowire.Number]loginResponseField)
	for len(raw) > 0 {
		number, wire, n := protowire.ConsumeTag(raw)
		if n < 0 || number < 1 {
			return nil, ErrLoginCodeMalformed
		}
		raw = raw[n:]
		field := loginResponseField{wire: wire}
		switch wire {
		case protowire.VarintType:
			field.n, n = protowire.ConsumeVarint(raw)
		case protowire.BytesType:
			field.b, n = protowire.ConsumeBytes(raw)
		default:
			n = protowire.ConsumeFieldValue(number, wire, raw)
		}
		if n < 0 {
			return nil, ErrLoginCodeMalformed
		}
		// The fields used here are singular. Unknown fields remain skippable.
		if number <= 3 {
			if _, duplicate := fields[number]; duplicate {
				return nil, ErrLoginCodeMalformed
			}
			fields[number] = field
		}
		raw = raw[n:]
	}
	return fields, nil
}

func parseLoginCodeResponse(raw []byte) ([]byte, error) {
	outer, err := loginResponseFields(raw)
	if err != nil {
		return nil, err
	}
	body, ok := outer[2]
	if !ok {
		return nil, ErrLoginCodeUnavailable
	}
	if body.wire != protowire.BytesType {
		return nil, ErrLoginCodeMalformed
	}
	fields, err := loginResponseFields(body.b)
	if err != nil {
		return nil, err
	}
	// JSLoginResponse: BaseResponse = 1, JSAPI Result = 2, code = 3.
	// A refusal wins even if a response also contains a code-shaped value.
	for _, status := range []struct {
		number protowire.Number
		stage  string
	}{{1, "jslogin_business"}, {2, "jslogin_result"}} {
		field, present := fields[status.number]
		if !present {
			continue
		}
		if field.wire != protowire.BytesType {
			return nil, ErrLoginCodeMalformed
		}
		base, err := loginResponseFields(field.b)
		if err != nil {
			return nil, err
		}
		if ret, present := base[1]; present {
			if ret.wire != protowire.VarintType {
				return nil, ErrLoginCodeMalformed
			}
			if code := int32(ret.n); code != 0 {
				return nil, &LoginCodeRejectedError{Stage: status.stage, Code: code}
			}
		}
	}
	code, ok := fields[3]
	if !ok || len(code.b) == 0 && code.wire == protowire.BytesType {
		return nil, ErrLoginCodeUnavailable
	}
	if code.wire != protowire.BytesType {
		return nil, ErrLoginCodeMalformed
	}
	return code.b, nil
}
