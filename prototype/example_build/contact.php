<?php
// Minimal PHP 7.4-compatible contact form handler. Test submissions carry _test=1 and go to the test inbox.
header('Content-Type: application/json');
$isTest = isset($_POST['_test']) && $_POST['_test'] === '1';
$to = $isTest ? 'test-inbox@example.test' : 'owner@example.test';
$name = isset($_POST['name']) ? strip_tags($_POST['name']) : '';
$msg = isset($_POST['message']) ? strip_tags($_POST['message']) : '';
$ok = $name !== '' && $msg !== '';
if ($ok && !$isTest) { @mail($to, 'Website enquiry from ' . $name, $msg); }
echo json_encode(array('ok' => $ok, 'test' => $isTest));
