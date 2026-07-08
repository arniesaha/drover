import paramiko
import os

def check_nas():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    key_path = "/Users/arnabmac/.ssh/id_ed25519"
    key = paramiko.Ed25519Key.from_private_key_file(key_path)
    
    try:
        client.connect(hostname="192.168.1.70", username="Arnab", pkey=key, timeout=10)
        
        # Check OpenClaw session paths
        cmds = [
            "echo '--- Checking OpenClaw Paths ---'",
            "ls -la ~/.openclaw/agents/main/sessions/ | head -n 5 || echo 'Not in ~/.openclaw'",
            "ls -la /home/Arnab/clawd/projects/openclaw/ | grep sessions || echo 'No sessions dir in clawd project'",
            
            "echo '\n--- Checking Kubeconfig ---'",
            "ls -la ~/.kube/config || echo 'No ~/.kube/config found'",
            "cat ~/.kube/config | grep server || echo 'No server in kubeconfig'",
            
            "echo '\n--- Checking k3s node status ---'",
            "sudo k3s kubectl get nodes || kubectl get nodes || echo 'Cannot run kubectl'"
        ]
        
        for cmd in cmds:
            stdin, stdout, stderr = client.exec_command(cmd)
            out = stdout.read().decode('utf-8')
            err = stderr.read().decode('utf-8')
            if out:
                print(out.strip())
            if err:
                print(f"Error: {err.strip()}")
                
    finally:
        client.close()

if __name__ == "__main__":
    check_nas()
