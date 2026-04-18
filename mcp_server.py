import os
import asyncio
import subprocess
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

server = Server("reports-service-codebase-mcp")

SUPPORTED_EXTENSIONS = (
    ".java", ".py", ".js", ".ts", ".jsx", ".tsx",
    ".xml", ".yml", ".yaml", ".json", ".properties",
    ".html", ".css", ".scss", ".sql", ".kt", ".groovy",
)


def count_lines_in_file(file_path):
    """Count lines in a single file."""
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            return len(f.readlines())
    except Exception:
        return 0


def analyze_directory(root_path, extensions=None):
    """Scan a directory tree and count files/lines grouped by extension."""
    if extensions is None:
        extensions = SUPPORTED_EXTENSIONS

    results = {}  # ext -> {"files": count, "lines": count, "file_list": [...]}

    for root, dirs, files in os.walk(root_path):
        for file in files:
            ext = os.path.splitext(file)[1].lower()
            if ext in extensions:
                file_path = os.path.join(root, file)
                lines = count_lines_in_file(file_path)
                if ext not in results:
                    results[ext] = {"files": 0, "lines": 0, "file_list": []}
                results[ext]["files"] += 1
                results[ext]["lines"] += lines
                results[ext]["file_list"].append(file_path)

    return results


def find_by_name(root_path, name):
    """Search for files matching a class/service name within a project directory."""
    matches = []
    name_lower = name.lower()

    for root, dirs, files in os.walk(root_path):
        for file in files:
            file_lower = file.lower()
            file_base = os.path.splitext(file_lower)[0]
            ext = os.path.splitext(file)[1].lower()
            if ext in SUPPORTED_EXTENSIONS and name_lower in file_base:
                file_path = os.path.join(root, file)
                lines = count_lines_in_file(file_path)
                matches.append({"path": file_path, "lines": lines})

    return matches


def format_directory_results(path, results):
    """Format directory scan results into a readable string."""
    total_files = sum(v["files"] for v in results.values())
    total_lines = sum(v["lines"] for v in results.values())

    lines = [
        f"Scanned: {path}",
        f"Total files: {total_files}",
        f"Total lines: {total_lines}",
        "",
        "Breakdown by type:",
    ]
    for ext in sorted(results.keys(), key=lambda e: results[e]["lines"], reverse=True):
        info = results[ext]
        lines.append(f"  {ext}: {info['files']} files, {info['lines']} lines")

    return "\n".join(lines)


MAX_FILE_SIZE = 100_000  # 100KB limit for reading files


def handle_read_file(arguments):
    path = arguments.get("path", "").strip()
    if not path:
        raise ValueError("'path' argument is required")

    if not os.path.isfile(path):
        return [TextContent(type="text", text=f"Error: File not found: {path}")]

    size = os.path.getsize(path)
    if size > MAX_FILE_SIZE:
        return [TextContent(
            type="text",
            text=f"Error: File too large ({size} bytes). Max allowed: {MAX_FILE_SIZE} bytes."
        )]

    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception as e:
        return [TextContent(type="text", text=f"Error reading file: {e}")]

    return [TextContent(type="text", text=f"File: {path}\n\n{content}")]


def handle_list_directory(arguments):
    path = arguments.get("path", "").strip()
    recursive = arguments.get("recursive", False)

    if not path:
        raise ValueError("'path' argument is required")

    if not os.path.isdir(path):
        return [TextContent(type="text", text=f"Error: Directory not found: {path}")]

    entries = []
    if recursive:
        for root, dirs, files in os.walk(path):
            for d in sorted(dirs):
                rel = os.path.relpath(os.path.join(root, d), path)
                entries.append(f"[DIR]  {rel}")
            for f in sorted(files):
                rel = os.path.relpath(os.path.join(root, f), path)
                entries.append(f"       {rel}")
        # Cap output to avoid huge responses
        if len(entries) > 500:
            entries = entries[:500]
            entries.append(f"\n... truncated ({len(entries)}+ entries)")
    else:
        items = sorted(os.listdir(path))
        for item in items:
            full = os.path.join(path, item)
            prefix = "[DIR]  " if os.path.isdir(full) else "       "
            entries.append(f"{prefix}{item}")

    header = f"Directory: {path}\n\n"
    return [TextContent(type="text", text=header + "\n".join(entries))]


def handle_search_in_files(arguments):
    pattern = arguments.get("pattern", "").strip()
    directory = arguments.get("directory", "").strip()
    file_ext = arguments.get("file_extension", "").strip()

    if not pattern:
        raise ValueError("'pattern' argument is required")
    if not directory:
        raise ValueError("'directory' argument is required")
    if not os.path.isdir(directory):
        return [TextContent(type="text", text=f"Error: Directory not found: {directory}")]

    extensions = (file_ext.lower(),) if file_ext else SUPPORTED_EXTENSIONS
    matches = []
    max_matches = 50

    for root, dirs, files in os.walk(directory):
        for file in files:
            ext = os.path.splitext(file)[1].lower()
            if ext not in extensions:
                continue
            file_path = os.path.join(root, file)
            try:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    for line_num, line in enumerate(f, 1):
                        if pattern.lower() in line.lower():
                            matches.append({
                                "file": file_path,
                                "line_num": line_num,
                                "line": line.rstrip()[:200]
                            })
                            if len(matches) >= max_matches:
                                break
            except Exception:
                continue
            if len(matches) >= max_matches:
                break
        if len(matches) >= max_matches:
            break

    if not matches:
        return [TextContent(
            type="text",
            text=f"No matches for '{pattern}' in {directory}"
        )]

    lines = [f"Found {len(matches)} match(es) for '{pattern}':", ""]
    for m in matches:
        lines.append(f"  {m['file']}:{m['line_num']}")
        lines.append(f"    {m['line']}")

    if len(matches) >= max_matches:
        lines.append(f"\n... showing first {max_matches} matches")

    return [TextContent(type="text", text="\n".join(lines))]


def handle_git_history(arguments):
    path = arguments.get("path", "").strip()
    max_commits = arguments.get("max_commits", 20)

    if not path:
        raise ValueError("'path' argument is required")

    if not os.path.exists(path):
        return [TextContent(type="text", text=f"Error: Path not found: {path}")]

    # Determine the working directory for git commands
    if os.path.isfile(path):
        cwd = os.path.dirname(path)
    else:
        cwd = path

    # Check if this is a git repo
    try:
        subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=cwd, capture_output=True, text=True, check=True
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return [TextContent(type="text", text=f"Error: Not a git repository: {cwd}")]

    # Build git log command
    cmd = [
        "git", "log",
        f"--max-count={max_commits}",
        "--pretty=format:%h | %ad | %an | %s",
        "--date=short",
    ]

    # If path is a specific file, add it
    if os.path.isfile(path):
        cmd.append("--")
        cmd.append(path)

    try:
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, check=True
        )
    except subprocess.CalledProcessError as e:
        return [TextContent(type="text", text=f"Git error: {e.stderr}")]

    if not result.stdout.strip():
        return [TextContent(type="text", text=f"No git history found for: {path}")]

    header = f"Git history for: {path}\n(showing up to {max_commits} commits)\n\n"
    header += "Hash     | Date       | Author          | Message\n"
    header += "-" * 70 + "\n"

    return [TextContent(type="text", text=header + result.stdout)]


def handle_git_diff(arguments):
    path = arguments.get("path", "").strip()
    commit = arguments.get("commit", "").strip()

    if not path:
        raise ValueError("'path' argument is required")

    if not os.path.exists(path):
        return [TextContent(type="text", text=f"Error: Path not found: {path}")]

    if os.path.isfile(path):
        cwd = os.path.dirname(path)
    else:
        cwd = path

    # Default: show uncommitted changes
    if commit:
        cmd = ["git", "show", "--stat", "--patch", commit]
        if os.path.isfile(path):
            cmd.extend(["--", path])
    else:
        cmd = ["git", "diff"]
        if os.path.isfile(path):
            cmd.extend(["--", path])

    try:
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, check=True
        )
    except subprocess.CalledProcessError as e:
        return [TextContent(type="text", text=f"Git error: {e.stderr}")]

    if not result.stdout.strip():
        label = f"commit {commit}" if commit else "working tree"
        return [TextContent(type="text", text=f"No changes in {label} for: {path}")]

    # Truncate if too large
    output = result.stdout
    if len(output) > MAX_FILE_SIZE:
        output = output[:MAX_FILE_SIZE] + "\n\n... truncated (output too large)"

    return [TextContent(type="text", text=output)]


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="analyze_codebase",
            description=(
                "Analyze a codebase by directory path, file path, or class/service name. "
                "Supports .java, .py, .js, .ts, .xml, .yml, .json, .html, .css, .sql, and more. "
                "If 'query' is a directory, scans all supported files recursively. "
                "If 'query' is a file, reports its line count. "
                "If 'query' is a name (e.g. 'AwsRegulatoryComplianceReportService'), "
                "searches within 'project_dir' for matching files."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "A directory path, file path, or class/service name to search for"
                        )
                    },
                    "project_dir": {
                        "type": "string",
                        "description": (
                            "Project root directory to search in when 'query' is a class/service name. "
                            "Optional if 'query' is already a full path."
                        )
                    }
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="read_file",
            description=(
                "Read and return the full contents of a file. "
                "Use this to view source code, config files, etc."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path to the file to read"
                    }
                },
                "required": ["path"]
            }
        ),
        Tool(
            name="list_directory",
            description=(
                "List files and subdirectories in a directory. "
                "Use this to browse and explore a project structure."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path to the directory to list"
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "If true, list all files recursively. Defaults to false (top-level only)."
                    }
                },
                "required": ["path"]
            }
        ),
        Tool(
            name="search_in_files",
            description=(
                "Search for a text pattern (substring or keyword) inside files in a directory. "
                "Returns matching file paths and the lines that contain the match."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Text or keyword to search for"
                    },
                    "directory": {
                        "type": "string",
                        "description": "Absolute path to the directory to search in"
                    },
                    "file_extension": {
                        "type": "string",
                        "description": "Optional file extension filter, e.g. '.java'. If omitted, searches all supported file types."
                    }
                },
                "required": ["pattern", "directory"]
            }
        ),
        Tool(
            name="git_history",
            description=(
                "Show git commit history for a file or repository. "
                "Returns commit hash, date, author, and message for each commit."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path to a file or git repository directory"
                    },
                    "max_commits": {
                        "type": "integer",
                        "description": "Maximum number of commits to return. Defaults to 20."
                    }
                },
                "required": ["path"]
            }
        ),
        Tool(
            name="git_diff",
            description=(
                "Show git diff for a file or repository. "
                "Without a commit hash, shows uncommitted changes. "
                "With a commit hash, shows what changed in that specific commit."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path to a file or git repository directory"
                    },
                    "commit": {
                        "type": "string",
                        "description": "Optional commit hash to show. If omitted, shows uncommitted changes."
                    }
                },
                "required": ["path"]
            }
        ),
    ]


@server.call_tool()
async def call_tool(name, arguments):
    if name == "read_file":
        return handle_read_file(arguments)
    elif name == "list_directory":
        return handle_list_directory(arguments)
    elif name == "search_in_files":
        return handle_search_in_files(arguments)
    elif name == "git_history":
        return handle_git_history(arguments)
    elif name == "git_diff":
        return handle_git_diff(arguments)
    elif name != "analyze_codebase":
        raise ValueError(f"Unknown tool: {name}")

    query = arguments.get("query", "").strip()
    project_dir = arguments.get("project_dir", "").strip()

    if not query:
        raise ValueError("'query' argument is required")

    # Case 1: query is an existing directory
    if os.path.isdir(query):
        results = analyze_directory(query)
        if not results:
            text = f"No supported files found in: {query}"
        else:
            text = format_directory_results(query, results)
        return [TextContent(type="text", text=text)]

    # Case 2: query is an existing file
    if os.path.isfile(query):
        lines = count_lines_in_file(query)
        ext = os.path.splitext(query)[1]
        text = f"File: {query}\nType: {ext}\nLines: {lines}"
        return [TextContent(type="text", text=text)]

    # Case 3: query is a class/service name — search within project_dir
    if not project_dir:
        return [TextContent(
            type="text",
            text=(
                f"'{query}' is not a valid path. "
                "To search by class/service name, also provide 'project_dir'."
            )
        )]

    if not os.path.isdir(project_dir):
        return [TextContent(
            type="text",
            text=f"Error: project_dir does not exist: {project_dir}"
        )]

    matches = find_by_name(project_dir, query)
    if not matches:
        return [TextContent(
            type="text",
            text=f"No files matching '{query}' found in: {project_dir}"
        )]

    lines = [f"Found {len(matches)} file(s) matching '{query}' in {project_dir}:", ""]
    for m in matches:
        lines.append(f"  {m['path']} — {m['lines']} lines")

    total = sum(m["lines"] for m in matches)
    lines.append(f"\nTotal lines across matches: {total}")

    return [TextContent(type="text", text="\n".join(lines))]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
