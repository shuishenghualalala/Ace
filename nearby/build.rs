use std::env;

fn main() {
    println!("cargo:rerun-if-changed=Info.plist");

    if env::var("CARGO_CFG_TARGET_OS").as_deref() != Ok("macos") {
        return;
    }

    let manifest_dir = env::var("CARGO_MANIFEST_DIR")
        .expect("CARGO_MANIFEST_DIR must be available to the Nearby build script");
    let info_plist = format!("{manifest_dir}/Info.plist");

    // Embed the privacy manifest in the CLI binary. CoreBluetooth reads this
    // section for non-bundled macOS executables as well as .app bundles.
    for argument in ["-sectcreate", "__TEXT", "__info_plist", info_plist.as_str()] {
        println!("cargo:rustc-link-arg={argument}");
    }
}
