use base64::Engine as _;
use minisign_verify::{PublicKey, Signature};
use serde_json::Value;
use std::{env, error::Error, fs, path::Path};

fn required<'a>(value: &'a Value, field: &str) -> Result<&'a str, Box<dyn Error>> {
    value.as_str().ok_or_else(|| format!("missing {field}").into())
}

fn decode(value: &str) -> Result<String, Box<dyn Error>> {
    Ok(String::from_utf8(base64::engine::general_purpose::STANDARD.decode(value)?)?)
}

fn main() -> Result<(), Box<dyn Error>> {
    let args: Vec<String> = env::args().collect();
    let [_, manifest_file, config_file, signed_file, published_file, sig_file, version] = &args[..]
    else {
        return Err("usage: pku-updater-verify <manifest> <tauri-config> <signed-installer> <published-installer> <sig-file> <version>".into());
    };

    let manifest: Value = serde_json::from_slice(&fs::read(manifest_file)?)?;
    let config: Value = serde_json::from_slice(&fs::read(config_file)?)?;
    let release = &manifest["windows-x86_64"];
    if required(&release["version"], "version")? != version {
        return Err("manifest version does not match installer version".into());
    }
    let published_name = Path::new(published_file)
        .file_name()
        .and_then(|name| name.to_str())
        .ok_or("invalid published installer name")?;
    if required(&release["installer"], "installer")? != published_name {
        return Err("manifest installer name does not match published file".into());
    }

    let generated_signature = fs::read_to_string(sig_file)?;
    let generated_signature = generated_signature.trim();
    let announced_signature = required(&release["signature"], "signature")?;
    if announced_signature != generated_signature {
        return Err("manifest signature must equal the .sig file contents exactly; do not encode it again".into());
    }

    let signed_bytes = fs::read(signed_file)?;
    if signed_bytes != fs::read(published_file)? {
        return Err("published installer differs from the signed installer".into());
    }
    let pubkey = required(&config["plugins"]["updater"]["pubkey"], "updater public key")?;
    let public_key = PublicKey::decode(&decode(pubkey)?)?;
    let signature = Signature::decode(&decode(generated_signature)?)?;
    public_key.verify(&signed_bytes, &signature, true)?;
    if !signature
        .trusted_comment()
        .split_whitespace()
        .any(|part| part == format!("version:{version}"))
    {
        return Err("signed version does not match manifest version".into());
    }

    println!("Updater manifest, installer bytes, signature, public key, and signed version verified");
    Ok(())
}
