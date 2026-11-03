/* Optional, ROCm only. Stubs for HSA symbols that a PyTorch ROCm wheel's bundled runtime (ROCR 1.21) expects but the
 * system runtime (1.18) lacks. Loaded together with the system libhsa-runtime64 through LD_PRELOAD, it stops the
 * runtime's idle core-spin (TheRock#7051). Every stub returns 1 (an HSA error status), so a code path that really
 * calls one fails loudly instead of misbehaving. Nothing has been observed calling them in the sidecar's workload.
 *
 *   gcc -shared -fPIC -o libhsa_shim.so hsa_shim.c
 */
int hsa_amd_agent_preload(void){return 1;}
int hsa_amd_counted_queue_acquire(void){return 1;}
int hsa_amd_counted_queue_release(void){return 1;}
int hsa_amd_external_semaphore_handle_close(void){return 1;}
int hsa_amd_external_semaphore_handle_open(void){return 1;}
int hsa_amd_memory_async_batch_copy(void){return 1;}
int hsa_amd_queue_create(void){return 1;}
int hsa_amd_signal_get_event_id(void){return 1;}
int hsa_amd_svm_discard_batch_async(void){return 1;}
int hsa_amd_vmem_export_fabric_handle(void){return 1;}
int hsa_amd_vmem_import_fabric_handle(void){return 1;}
int hsa_ext_image_create_v2(void){return 1;}
int hsa_ext_image_data_get_info_v2(void){return 1;}
int hsa_ext_image_destroy_v2(void){return 1;}
int hsa_ext_image_mipmap_array_get_level(void){return 1;}
