// Intentionally broken negative control. The demo fixes only its sandbox copy.
fn count_items(mut items: Vec<String>) -> usize {
    let mut count = 0;
    while !items.is_empty() {
        debug_assert!(items.pop().is_some());
        count += 1;
    }
    count
}

fn main() {
    println!("{}", count_items(std::env::args().skip(1).collect()));
}

#[cfg(test)]
mod tests {
    use super::count_items;

    #[test]
    fn empty() {
        assert_eq!(count_items(vec![]), 0);
    }

    #[test]
    fn nonempty() {
        if let Ok(build) = std::env::var("ORCH_BUILD_DIR") {
            std::fs::write(
                std::path::Path::new(&build).join("test.pid"),
                std::process::id().to_string(),
            )
            .expect("write fixture process witness");
            std::fs::write(
                std::path::Path::new(&build).join("test.stat"),
                std::fs::read_to_string("/proc/self/stat").expect("Linux fixture process state"),
            )
            .expect("write fixture process-group witness");
        }
        assert_eq!(
            count_items(vec!["alpha".into(), "beta".into(), "gamma".into()]),
            3
        );
    }
}
