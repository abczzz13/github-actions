# Decodes the payload of a JWT string without verifying it. The API server
# verifies the token. JWTs use unpadded base64url, and @base64d needs
# standard base64 with padding.
def jwt_claims:
    split(".")[1] | gsub("-"; "+") | gsub("_"; "/")
    | . + ("=" * ((4 - length % 4) % 4))
    | @base64d | fromjson;
